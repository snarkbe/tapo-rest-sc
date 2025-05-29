from flask import Flask, jsonify, redirect, url_for
import requests
import json
import os
import logging

# --- Flask App Setup ---
app = Flask(__name__)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Global Variables for Configuration and State ---
TAPO_API_URL = None
AUTH_TOKEN = None
DEVICES_LIST = []
INITIALIZATION_ERROR = None
LOGIN_PASSWORD = None

# --- Login and get token ---
def login_tapo_rest():

    login_url = f"{TAPO_API_URL}/login"
    login_payload = {"password": LOGIN_PASSWORD}
    login_headers = {"Content-Type": "application/json"}
    token = None
    
    try:
        response = requests.post(login_url, headers=login_headers, json=login_payload)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        
        token = response.text.strip()
        
        if not token:
            print(f"Error: Login token is empty. Raw response: '{response.text}'")
            exit()
        logging.info(f"Login successful. Token: {token}")
        return token
        
    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error during login: {http_err}")
        if http_err.response is not None:
            print(f"Response content: {http_err.response.text}")
        return
    except requests.exceptions.ConnectionError as conn_err:
        print(f"Connection error during login: {conn_err}")
        return
    except requests.exceptions.Timeout as timeout_err:
        print(f"Timeout error during login: {timeout_err}")
        return
    except requests.exceptions.RequestException as req_err:
        print(f"Error during login: {req_err}")
        return

# --- Helper Function to Load JSON Files ---
def load_json_file(file_path, description="file"):
    """Loads a JSON file and returns its content or an error message."""
    try:
        with open(file_path, 'r') as f:
            if "devices.json" in file_path: # Specific handling for potential comment in devices.json
                first_line = f.readline()
                if not first_line.strip().startswith("//"):
                    f.seek(0)
            return json.load(f), None
    except FileNotFoundError:
        return None, f"Error: {description.capitalize()} file not found at {file_path}"
    except json.JSONDecodeError:
        return None, f"Error: Could not decode JSON from {file_path}"
    except Exception as e:
        return None, f"An unexpected error occurred while reading {file_path}: {e}"

# --- Initialization Functions (called once at startup) ---
def load_configuration():
    """Loads tapo_api_url and auth_token from config.json."""
    global TAPO_API_URL, AUTH_TOKEN, INITIALIZATION_ERROR, LOGIN_PASSWORD

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file_path = os.path.join(script_dir, "app", "config.json")
    
    config_data_loaded, error = load_json_file(config_file_path, "config")
    if error:
        INITIALIZATION_ERROR = error
        logging.error(INITIALIZATION_ERROR)
        return

    TAPO_API_URL = config_data_loaded.get("tapo_api_url")
    # Use login_password if auth_token is not available
    LOGIN_PASSWORD = config_data_loaded.get("login_password")

    if not TAPO_API_URL or not LOGIN_PASSWORD:
        INITIALIZATION_ERROR = "Error: 'tapo_api_url' or 'login_password' not found in config.json."
        logging.error(INITIALIZATION_ERROR)
    else:
        logging.info("Configuration loaded successfully.")

def load_devices():
    """Loads the list of devices from devices.json."""
    global DEVICES_LIST, INITIALIZATION_ERROR

    if INITIALIZATION_ERROR: return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    devices_file_path = os.path.join(script_dir, "app", "devices.json")

    devices_data_loaded, error = load_json_file(devices_file_path, "devices")
    if error:
        INITIALIZATION_ERROR = error
        logging.error(INITIALIZATION_ERROR)
        return
    
    DEVICES_LIST = devices_data_loaded.get("devices", [])
    if not DEVICES_LIST:
        INITIALIZATION_ERROR = "No devices found in devices.json or the 'devices' key is missing/empty."
        logging.error(INITIALIZATION_ERROR)
    else:
        logging.info(f"Loaded {len(DEVICES_LIST)} device(s) from devices.json.")

# --- Perform Initialization ---
load_configuration()
if not INITIALIZATION_ERROR:
    AUTH_TOKEN = login_tapo_rest()
    load_devices()


# --- API Call Function with Simplified Error Handling ---
def fetch_device_power_data_with_auth(device_name, device_type):
    """
    Fetches power data from the Tapo API using the pre-configured auth token.
    Error handling is simplified.
    """
    if not AUTH_TOKEN:
        return {"device": device_name, "error": "Authentication token not available.", "status": "failed"}

    device_type_lower = device_type.lower()
    power_url = f"{TAPO_API_URL}/actions/{device_type_lower}/get-current-power"
    params = {'device': device_name}
    power_headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}

    try:
        response = requests.get(power_url, headers=power_headers, params=params, timeout=10)
        response.raise_for_status()  # Raises HTTPError for 4xx/5xx responses
        current_power_data = response.json() # Raises ValueError/JSONDecodeError for invalid JSON
        return {"device": device_name, "data": current_power_data, "status": "success"}
    
    except requests.exceptions.HTTPError as http_err:
        # Specific handling for HTTP errors to include status code and response text if possible
        status_code = "N/A"
        error_text = str(http_err)
        if http_err.response is not None:
            status_code = http_err.response.status_code
            error_text = http_err.response.text[:200] # Limit error text length
        logging.error(f"HTTPError for {device_name}: {status_code} - {http_err}") # Log full error
        return {"device": device_name, "error": f"HTTP error {status_code}", "details": error_text, "status": "failed"}

    except requests.exceptions.RequestException as req_err:
        # Catches other request-related errors (ConnectionError, Timeout, etc.)
        logging.error(f"RequestException for {device_name}: {req_err}") # Log full error
        return {"device": device_name, "error": f"Request failed: {type(req_err).__name__}", "status": "failed"}
    
    except (ValueError, json.JSONDecodeError) as json_err: # Handles errors from response.json()
        logging.error(f"JSONDecodeError for {device_name}: {json_err}") # Log full error
        return {"device": device_name, "error": "Invalid JSON response from API.", "status": "failed"}
        
    except Exception as e: # Catch any other unexpected error
        logging.error(f"Unexpected error for {device_name}: {e}", exc_info=True) # Log full error with traceback
        return {"device": device_name, "error": f"An unexpected error occurred: {type(e).__name__}", "status": "failed"}


# --- Flask API Endpoint ---
@app.route('/get_all_device_power', methods=['GET'])
def get_all_device_power():
    """
    Flask endpoint. Queries power data for configured devices.
    """
    if INITIALIZATION_ERROR:
        logging.error(f"API call failed due to initialization error: {INITIALIZATION_ERROR}")
        return jsonify({"error": "Server initialization failed. Check logs.", "details": INITIALIZATION_ERROR}), 500
        
    results = []
    for device_info in DEVICES_LIST:
        device_name = device_info.get("name")
        device_type = device_info.get("device_type")

        if not device_name or not device_type:
            logging.warning(f"Skipping device due to missing 'name' or 'device_type': {device_info}")
            results.append({
                "device_info": device_info, 
                "error": "Missing 'name' or 'device_type' in devices.json entry", 
                "status": "skipped"
            })
            continue
        
        result = fetch_device_power_data_with_auth(device_name, device_type)
        results.append(result)

    return jsonify(results), 200

# default route
@app.route('/')
def default_route():
    return redirect(url_for('get_all_device_power'))

# --- Run the Flask Development Server ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
    
# Note: In production, consider using a WSGI server like Gunicorn or uWSGI instead of Flask's built-in server.
# This is a development server and should not be used in production.
# The debug=True flag is useful for development but should be set to False in production.
# Note: Ensure that the TAPO_API_URL and AUTH_TOKEN are correctly set in config.json.
