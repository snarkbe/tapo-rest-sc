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
        
        # Debug logging for API response structure
        logging.debug(f"API response for {device_name}: {json.dumps(current_power_data, indent=2)}")
        
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
    If a device has a "substract" field, its value (name of another device)
    will have its power subtracted from the current device's power.
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
    
    # Process subtractions after all device data is collected
    # Create a device name to result mapping for easy lookup
    results_by_name = {result.get("device"): result for result in results if result.get("device")}
    
    for device_info in DEVICES_LIST:
        device_name = device_info.get("name")
        subtract_device_name = device_info.get("substract")
        
        if subtract_device_name and device_name in results_by_name:
            main_result = results_by_name[device_name]
            
            # Skip if main device didn't get successful data
            if main_result.get("status") != "success" or "data" not in main_result:
                logging.warning(f"Cannot apply subtraction for '{device_name}': No valid power data available")
                continue
                
            # Check if subtracted device exists and has valid data
            if subtract_device_name not in results_by_name:
                logging.warning(f"Cannot apply subtraction for '{device_name}': Device '{subtract_device_name}' not found")
                main_result["data"]["subtraction_error"] = f"Device to subtract '{subtract_device_name}' not found"
                continue
                
            subtract_result = results_by_name[subtract_device_name]
            if subtract_result.get("status") != "success" or "data" not in subtract_result:
                logging.warning(f"Cannot apply subtraction for '{device_name}': No valid power data for '{subtract_device_name}'")
                main_result["data"]["subtraction_error"] = f"No valid power data for '{subtract_device_name}'"
                continue
              # Get power values and subtract
            try:
                # Debug logging to help understand the data structure
                logging.debug(f"Main device data structure: {json.dumps(main_result.get('data', {}), indent=2)}")
                logging.debug(f"Subtract device data structure: {json.dumps(subtract_result.get('data', {}), indent=2)}")
                
                # Check if the expected structure exists in the response
                main_data = main_result.get("data", {})
                subtract_data = subtract_result.get("data", {})
                
                # Try to get current_power from the response, handle different data structures
                main_power = None
                subtract_power = None
                
                # Try different paths where current_power might be found
                if "result" in main_data and "current_power" in main_data["result"]:
                    main_power = main_data["result"]["current_power"]
                elif "current_power" in main_data:
                    main_power = main_data["current_power"]
                
                if "result" in subtract_data and "current_power" in subtract_data["result"]:
                    subtract_power = subtract_data["result"]["current_power"]
                elif "current_power" in subtract_data:
                    subtract_power = subtract_data["current_power"]
                
                if main_power is None or subtract_power is None:
                    raise KeyError(f"Could not find current_power in the data. Main power found: {main_power is not None}, Subtract power found: {subtract_power is not None}")
                
                if isinstance(main_power, (int, float)) and isinstance(subtract_power, (int, float)):
                    original_power = main_power
                    adjusted_power = max(0, main_power - subtract_power)  # Ensure power doesn't go negative
                    
                    # Update the power value 
                    # Store at the same location where we found it
                    if "result" in main_data and "current_power" in main_data["result"]:
                        main_result["data"]["result"]["current_power"] = adjusted_power
                    elif "current_power" in main_data:
                        main_result["data"]["current_power"] = adjusted_power
                    
                    # Add subtraction info
                    main_result["data"]["subtraction_info"] = {
                        "original_power": original_power,
                        "subtracted_device": subtract_device_name,
                        "subtracted_power": subtract_power,
                        "adjusted_power": adjusted_power
                    }
                    logging.info(f"Applied subtraction for '{device_name}': {original_power} - {subtract_power} = {adjusted_power}")
                else:
                    logging.warning(f"Cannot apply subtraction for '{device_name}': Power values not numeric")
                    main_result["data"]["subtraction_error"] = "Power values not numeric"
            except KeyError as e:
                logging.warning(f"Cannot apply subtraction for '{device_name}': Missing power data fields - {e}")
                main_result["data"]["subtraction_error"] = f"Missing power data fields: {e}"
            except Exception as e:
                logging.error(f"Error applying subtraction for '{device_name}': {e}", exc_info=True)
                main_result["data"]["subtraction_error"] = f"Error during subtraction: {str(e)}"

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
