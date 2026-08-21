import time
import json
import ubinascii
import machine
import network
from umqtt.simple import MQTTClient
from secrets import WIFI_SSID, WIFI_PASSWORD, MQTT_USER, MQTT_PASSWORD
from tiny_fx import TinyFX

tiny = TinyFX() 
wlan = network.WLAN(network.STA_IF)

# ==========================================
# 1. CONFIGURATION (Change these values!)
# ==========================================
MQTT_BROKER   = "192.168.0.132"  # Your Home Assistant IP

# Status LED colours and flash rate
RED   = (255, 0, 0)
BLUE  = (0, 0, 255)
OFF   = (0, 0, 0)
FLASH_INTERVAL = 0.2

# Global variables to track light status
light_on = False
current_rgb = [255, 255, 255] # Default to White
current_brightness = 255       # Default Max

def drive_pins(r, g, b):
    colour=tuple([r,g,b])
    tiny.rgb.set_rgb(*colour)

def flash_wait(seconds, color, condition=None):
    # Flash `color` on/off at FLASH_INTERVAL for up to `seconds`, returning
    # early if `condition()` becomes true so we don't overshoot a connect.
    steps = max(1, int(seconds / FLASH_INTERVAL))
    on = False
    for _ in range(steps):
        on = not on
        drive_pins(*(color if on else OFF))
        time.sleep(FLASH_INTERVAL)
        if condition and condition():
            return True
    return False


# ==========================================
# 3. HOME ASSISTANT MQTT CALLBACK HANDLER
# ==========================================
def mqtt_callback(topic, msg):
    global light_on, current_rgb, current_brightness
    
    try:
        # Parse incoming Home Assistant JSON message
        data = json.loads(msg.decode('utf-8'))
        print("Received payload:", data)
        
        if "state" in data:
            light_on = (data["state"] == "ON")
            
        if "color" in data and "r" in data["color"]:
            current_rgb[0] = data["color"]["r"]
            current_rgb[1] = data["color"]["g"]
            current_rgb[2] = data["color"]["b"]
            
        if "brightness" in data:
            current_brightness = data["brightness"]
            
        # Execute the hardware adjustment
        if light_on:
            # Scale colors by brightness percentage
            factor = current_brightness / 255.0
            r_final = int(current_rgb[0] * factor)
            g_final = int(current_rgb[1] * factor)
            b_final = int(current_rgb[2] * factor)
            drive_pins(r_final, g_final, b_final)
        else:
            drive_pins(0, 0, 0) # Turn all channels OFF
            
        # Echo the final state back to HA so dashboard mirrors reality instantly
        state_payload = {
            "state": "ON" if light_on else "OFF",
            "brightness": current_brightness,
            "color": {"r": current_rgb[0], "g": current_rgb[1], "b": current_rgb[2]}
        }
        client.publish(state_topic, json.dumps(state_payload).encode('utf-8'), retain=True)

    except Exception as e:
        print("Failed to process MQTT payload:", e)

# ==========================================
# 4. NETWORK & REGISTRATION FUNCTIONS
# ==========================================
def connect_wifi():
    # Thonny soft-resets (Ctrl-D) instead of power-cycling, which can leave the
    # radio in a half-connected state from the previous run. Reset it first.
    wlan.active(False)
    time.sleep(1)
    wlan.active(True)
    wlan.config(pm=0xa11140)  # Disable power-save mode; it's known to drop the DHCP reply, leaving the link stuck at STAT_NOIP (status 2)
    network.hostname("tigerlight")

    if not wlan.isconnected():
        print(f"Connecting to Wi-Fi: {WIFI_SSID}...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)

        max_wait = 20
        while max_wait > 0 and wlan.status() != network.STAT_GOT_IP:
            max_wait -= 1
            print("Waiting for connection... status =", wlan.status())
            if flash_wait(1, RED, lambda: wlan.status() == network.STAT_GOT_IP):
                break

        if wlan.status() != network.STAT_GOT_IP:
            raise RuntimeError(f"Wi-Fi connection failed, status: {wlan.status()}")

    print("Wi-Fi Connected! IP Address:", wlan.ifconfig()[0])

# Generate identifiers for MQTT and Auto-Discovery
mac = ubinascii.hexlify(machine.unique_id()).decode('utf-8')
device_id = f"tiger_{mac}"

discovery_topic = f"homeassistant/light/{device_id}/light/config"
command_topic   = f"homeassistant/light/{device_id}/set"
state_topic     = f"homeassistant/light/{device_id}/state"

discovery_payload = {
    "name": "Tiger Light",
    "unique_id": device_id,
    "command_topic": command_topic,
    "state_topic": state_topic,
    "schema": "json",
    "color_mode": True,
    "supported_color_modes": ["rgb"],
    "brightness": True,
    "device": {
        "identifiers": [device_id],
        "name": "Tiger Light",
        "model": "Tiny FX",
        "manufacturer": "Ade"
    }
}

# ==========================================
# 5. MAIN LOOP PROCESS
# ==========================================
def connect_mqtt(max_attempts=5):
    # Right after Wi-Fi reports an IP, the router/AP hasn't always finished
    # settling routes, so the first TCP connect to the broker can get
    # ECONNABORTED. Retry in place rather than letting one blip fall through
    # to the outer except and hard-reset the board.
    client = MQTTClient(device_id, MQTT_BROKER, user=MQTT_USER, password=MQTT_PASSWORD)
    client.set_callback(mqtt_callback)
    for attempt in range(1, max_attempts + 1):
        drive_pins(*BLUE)
        try:
            client.connect()
            return client
        except OSError as e:
            print(f"MQTT connect attempt {attempt}/{max_attempts} failed: {e}")
            flash_wait(2, BLUE)
    raise RuntimeError("Could not connect to MQTT broker")

connect_wifi()

try:
    # Initialize the secure MQTT client with credentials
    print("Connecting to MQTT broker...")
    client = connect_mqtt()
    print("Connected to MQTT Broker.")
    
    # Register with Home Assistant Auto-Discovery
    client.publish(discovery_topic, json.dumps(discovery_payload).encode('utf-8'), retain=True)
    print("Auto-discovery profile registered.")
    
    # Start listening to commands
    client.subscribe(command_topic)
    
    # Initialize hardware state to matching off parameters
    drive_pins(0, 0, 0)
    initial_state = {"state": "OFF"}
    client.publish(state_topic, json.dumps(initial_state).encode('utf-8'), retain=True)
    
    print("System active! Adjust tiger in Home Assistant.")
    
    while True:
        # Constantly check for incoming color changes from the dashboard
        client.check_msg()
        time.sleep(0.1)

except Exception as e:
    print("Connection lost or loop crashed. Resetting board...", e)
    time.sleep(5)
    machine.reset()
    
# Turn off all the outputs
finally:
    tiny.shutdown()
    wlan.disconnect()
