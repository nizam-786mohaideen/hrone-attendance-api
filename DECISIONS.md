# Technical Decisions

## Framework
FastAPI was selected to build REST API endpoints and provide interactive API documentation.

## Database
MongoDB was selected to store employee details and attendance records.

## Configuration
Environment variables are used for database configuration. The .env file must not be committed to the repository.

## Attendance Calculations
Attendance calculations are separated into a dedicated module to keep the logic reusable and easier to test.

## Timestamps
Attendance timestamps use milliseconds since the Unix epoch in API requests and responses.

## Testing
Pytest is used to test attendance calculations and important edge cases.

## Security
Secrets and local development files must be excluded from version control.
