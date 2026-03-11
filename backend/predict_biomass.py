import joblib
import pandas as pd

# Load trained model
model = joblib.load("biomass_model.pkl")

# Example new location features
new_data = pd.DataFrame({
    "NDVI": [0.4008359536774292],
    "EVI": [0.44671194707662143],
    "Elevation": [7.133824825286865],
    "Slope": [0.036131199065771766]
})

prediction = model.predict(new_data)

print("Predicted Biomass:", prediction[0], "tons/hectare")

# 0.4008359536774292,0.44671194707662143,7.133824825286865,0.036131199065771766, result : 22.167116165161133