#!/bin/sh
# Start tapo-rest in the background
nohup ./tapo-rest /app/devices.json --port=80 &

# Wait a few seconds to ensure tapo-rest is up
sleep 2

# Start the waitress server
exec waitress-serve --host=0.0.0.0 --port=5000 taposc:app
