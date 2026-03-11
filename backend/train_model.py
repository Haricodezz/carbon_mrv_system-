import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error
import joblib

# Load dataset
df = pd.read_csv("Training_dataset.csv")

print("Dataset loaded:", len(df), "rows")

# Features (inputs)
X = df[["NDVI", "EVI", "Elevation", "Slope"]]

# Target (output)
y = df["Biomass"]

# Split into train and test sets
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)

# Create model
model = xgb.XGBRegressor(
    n_estimators=200,
    learning_rate=0.1,
    max_depth=6
)

# Train model
model.fit(X_train, y_train)

print("Model training complete")

# Test model
predictions = model.predict(X_test)

# Evaluate accuracy
mae = mean_absolute_error(y_test, predictions)

print("Mean Absolute Error:", mae)

# Save model
joblib.dump(model, "biomass_model.pkl")

print("Model saved as biomass_model.pkl")