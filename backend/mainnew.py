import ee
import pandas as pd
from gee_auth import initialize_earth_engine

# ---------------------------------------------------------------------------
# 1. Initialize Earth Engine (uses service account in production)
# ---------------------------------------------------------------------------
initialize_earth_engine()

# Expanded Area of Interest (AOI) to ensure we hit the 2000+ point target
# This covers a much larger section of the Sundarbans mangrove region.



aoi1 = ee.Geometry.Rectangle([88.0, 21.5, 89.5, 22.5])
# State: West Bengal
# Ecosystem: Mangrove
# Biomass level: Very High
aoi2 = ee.Geometry.Rectangle([86.5, 20.5, 87.5, 21.5])
# State: Odisha
# Ecosystem: Mangrove
# Biomass level: High
aoi3 = ee.Geometry.Rectangle([81.5, 15.5, 82.5, 16.5])
# State: Andhra Pradesh
# Ecosystem: Mangrove
# Biomass level: High
aoi4 = ee.Geometry.Rectangle([79.7, 11.3, 79.9, 11.6])
# State: Tamil Nadu
# Ecosystem: Mangrove
# Biomass level: Medium–High

aoi5 = ee.Geometry.Rectangle([92.3, 11.5, 93.3, 12.5])
# Region: Andaman Islands
# Ecosystem: Mangrove
# Biomass level: Very High
aoi6 = ee.Geometry.Rectangle( [75.0, 10.0, 77.0, 12.0])
#   ("Western Ghats", [75.0, 10.0, 77.0, 12.0]),
aoi7 = ee.Geometry.Rectangle( [78.0, 29.0, 80.0, 31.0])
#    ("Uttarakhand forest", [78.0, 29.0, 80.0, 31.0]),
aoi8 = ee.Geometry.Rectangle( [76.0, 9.5, 77.0, 11.0])
#   ("Kerala plantations", [76.0, 9.5, 77.0, 11.0]),
aoi9 = ee.Geometry.Rectangle( [75.0, 30.0, 76.0, 31.0])
#  ("Punjab farmland", [75.0, 30.0, 76.0, 31.0]),
aoi10 = ee.Geometry.Rectangle(  [78.0, 21.0, 80.0, 23.0])
#  ("Central India forest", [78.0, 21.0, 80.0, 23.0]),,
aoi11 = ee.Geometry.Rectangle( [92.0, 11.0, 94.0, 13.0])
# ("Andaman", [92.0, 11.0, 94.0, 13.0])
# aoi7 not done
aoi=aoi8


# Expanded time window to capture maximum GEDI laser tracks (2019-2023)
start_date = '2019-01-01'
end_date = '2023-12-31'

# ---------------------------------------------------------------------------
# 2. Prepare Satellite Datasets
# ---------------------------------------------------------------------------

# A. Sentinel-2 Harmonized (Surface Reflectance)
s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
      .filterBounds(aoi)
      .filterDate(start_date, end_date)
      .median()
      .divide(10000)) # Scale to 0-1 for EVI calculation

# Calculate NDVI: (B8 - B4) / (B8 + B4)
ndvi = s2.normalizedDifference(['B8', 'B4']).rename('NDVI')

# Calculate EVI: 2.5 * ((B8 - B4) / (B8 + 6*B4 - 7.5*B2 + 1))
evi = s2.expression(
    '2.5 * ((B8 - B4) / (B8 + 6 * B4 - 7.5 * B2 + 1))', {
        'B8': s2.select('B8'),
        'B4': s2.select('B4'),
        'B2': s2.select('B2')
    }).rename('EVI')

# B. Copernicus DEM (FIXED)
# GLO30 is an ImageCollection, so we mosaic it into a single continuous image.
dem_image = ee.ImageCollection("COPERNICUS/DEM/GLO30").select('DEM').mosaic()
elevation = dem_image.rename('Elevation')
slope = ee.Terrain.slope(dem_image).rename('Slope')

# Stack all predictors into a single multi-band image for efficient extraction
predictors = ee.Image.cat([ndvi, evi, elevation, slope])

# C. GEDI LiDAR Biomass
# Extract Above Ground Biomass Density (agbd)
gedi = (ee.ImageCollection("LARSE/GEDI/GEDI04_A_002_MONTHLY")
        .filterBounds(aoi)
        .select('agbd')
        .mean()) # Aggregate to get a stable biomass layer

# ---------------------------------------------------------------------------
# 3. Sample Valid GEDI Biomass Points
# ---------------------------------------------------------------------------
print("Sampling GEDI biomass points within expanded AOI...")
gedi_samples = gedi.sample(
    region=aoi,
    scale=30,
    numPixels=2000,    # Requesting 3500 to safely guarantee we net at least 2000 clean ones
    geometries=True,   # Required to get the coordinates
    dropNulls=True     # Drops points where GEDI data is missing
)

# Fetch the sampled points from Earth Engine to the local Python environment
features = gedi_samples.getInfo()['features']
initial_point_count = len(features)
print(f"-> Found {initial_point_count} valid GEDI points.")

# ---------------------------------------------------------------------------
# 4 & 5. Point-by-Point Extraction using reduceRegion
# ---------------------------------------------------------------------------
print("Extracting predictor variables point-by-point...")
print("(This may take a few minutes due to the high volume of API calls. Please wait.)")

dataset_rows = []

for i, feat in enumerate(features):
    # Print a progress update every 200 points
    if i % 200 == 0 and i > 0:
        print(f"Processed {i} / {initial_point_count} points...")

    # Extract exact coordinates and Biomass from the GEDI feature
    geom = feat['geometry']
    lon, lat = geom['coordinates']
    biomass_val = feat['properties'].get('agbd')
    
    # Create an Earth Engine Point geometry using the exact coordinates
    point = ee.Geometry.Point([lon, lat])
    
    # Extract NDVI, EVI, Elevation, and Slope at this exact point
    extracted_stats = predictors.reduceRegion(
        reducer=ee.Reducer.first(),
        geometry=point,
        scale=30
    ).getInfo()
    
    # Compile the row
    row = {
        'Longitude': lon,
        'Latitude': lat,
        'NDVI': extracted_stats.get('NDVI'),
        'EVI': extracted_stats.get('EVI'),
        'Elevation': extracted_stats.get('Elevation'),
        'Slope': extracted_stats.get('Slope'),
        'Biomass': biomass_val
    }
    dataset_rows.append(row)

# ---------------------------------------------------------------------------
# 6, 7 & 8. Format, Filter, and Save the Dataset
# ---------------------------------------------------------------------------
# Convert to Pandas DataFrame
df = pd.DataFrame(dataset_rows)

# Filter out any rows that contain None or NaN values
df_clean = df.dropna()

import os

output_filename = "Training_dataset.csv"

# Check if file exists
if not os.path.exists(output_filename):

    # File does not exist → create new file
    df_clean.to_csv(output_filename, index=False)

    print(f"Created new dataset file: {output_filename}")

else:

    # File exists → append data without writing header again
    df_clean.to_csv(output_filename, mode='a', header=False, index=False)

    print(f"Appended data to existing file: {output_filename}")

# ---------------------------------------------------------------------------
# 9. Print Final Outputs
# ---------------------------------------------------------------------------
print(f"Total GEDI points initially found: {initial_point_count}")
print(f"Total valid training rows collected (after removing Nulls): {len(df_clean)}")
print(f"Dataset saved to: {output_filename}\n")
print("First 5 rows of the dataset:")
print(df_clean.head())