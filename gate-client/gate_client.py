import os
import time
import logging
import serial
from gpiozero import Button, OutputDevice
from dotenv import load_dotenv

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class RanchGateMonitor:
    """Monitors a physical gate, transmits state changes, and accepts idempotent remote commands."""
    
    # Constants for LoRa Messages
    MSG_GATE_OPEN = "ALERT: Gate OPEN"
    MSG_GATE_CLOSED = "STATUS: Gate CLOSED"

    def __init__(self, serial_port: str, baud_rate: int, sensor_pin: int, relay_pin: int):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.sensor_pin = sensor_pin
        self.relay_pin = relay_pin
        
        self.lora = None
        self.gate_sensor = None
        self.gate_relay = None

    def setup(self) -> None:
        """Initializes hardware connections."""
        logger.info("Initializing LoRa connection on %s at %d baud...", self.serial_port, self.baud_rate)
        try:
            self.lora = serial.Serial(self.serial_port, self.baud_rate, timeout=1)
        except serial.SerialException as e:
            logger.error("Failed to connect to LoRa module: %s", e)
            raise

        logger.info("Initializing Gate Sensor on GPIO %d...", self.sensor_pin)
        self.gate_sensor = Button(self.sensor_pin, pull_up=True, bounce_time=0.5)
        
        # Register hardware callbacks for physical movement
        self.gate_sensor.when_released = self.on_gate_opened
        self.gate_sensor.when_pressed = self.on_gate_closed

        logger.info("Initializing GTO Remote Relay on GPIO %d...", self.relay_pin)
        self.gate_relay = OutputDevice(self.relay_pin, active_high=True, initial_value=False)

    def send_lora_message(self, message: str) -> None:
        """Transmits a message over the LoRa serial connection."""
        if self.lora and self.lora.is_open:
            try:
                logger.info("Transmitting: %s", message)
                self.lora.write((message + "\n").encode('utf-8'))
            except Exception as e:
                logger.error("Transmission failed: %s", e)
        else:
            logger.warning("LoRa connection is offline. Cannot send message.")

    def on_gate_opened(self) -> None:
        """Callback triggered when the magnetic circuit breaks."""
        logger.warning("Magnet separated! Gate is OPEN.")
        self.send_lora_message(self.MSG_GATE_OPEN)

    def on_gate_closed(self) -> None:
        """Callback triggered when the magnetic circuit is completed."""
        logger.info("Magnet detected! Gate is CLOSED.")
        self.send_lora_message(self.MSG_GATE_CLOSED)

    def trigger_remote_relay(self) -> None:
        """Fires the relay to simulate a physical button press on the GTO remote."""
        logger.info("Activating GTO Remote via Relay...")
        self.gate_relay.on()
        time.sleep(1.0)  
        self.gate_relay.off()
        logger.info("Remote Released.")

    def process_incoming_commands(self) -> None:
        """Reads the serial buffer and executes state-validated commands."""
        if self.lora.in_waiting > 0:
            try:
                raw_data = self.lora.readline().decode('utf-8', errors='ignore').strip()
                if not raw_data:
                    return
                    
                logger.info("LoRa Command Received: %s", raw_data)
                
                # --- Routine Status Ping ---
                if "CMD:STATUS_REQ" in raw_data:
                    if self.gate_sensor.is_pressed:
                        self.send_lora_message(self.MSG_GATE_CLOSED)
                    else:
                        self.send_lora_message(self.MSG_GATE_OPEN)
                        
                # --- State-Validated OPEN Command ---
                elif "CMD:OPEN" in raw_data:
                    if self.gate_sensor.is_pressed: 
                        # Magnet is touching -> Gate is closed. Safe to open.
                        logger.info("Sanity Check Passed: Gate is CLOSED. Proceeding to OPEN.")
                        self.trigger_remote_relay()
                        # The physical sensor separating will automatically trigger the "ALERT: OPEN" message
                    else:
                        logger.warning("Sanity Check Failed: Gate is already OPEN. Ignoring command.")
                        self.send_lora_message("ACK: Gate is already OPEN")

                # --- State-Validated CLOSE Command ---
                elif "CMD:CLOSE" in raw_data:
                    if not self.gate_sensor.is_pressed: 
                        # Magnet is separated -> Gate is open. Safe to close.
                        logger.info("Sanity Check Passed: Gate is OPEN. Proceeding to CLOSE.")
                        self.trigger_remote_relay()
                        # The physical sensor connecting will automatically trigger the "STATUS: CLOSED" message
                    else:
                        logger.warning("Sanity Check Failed: Gate is already CLOSED. Ignoring command.")
                        self.send_lora_message("ACK: Gate is already CLOSED")
                        
            except Exception as e:
                logger.error("Error reading from serial port: %s", e)

    def run(self) -> None:
        """Main execution loop to process incoming LoRa data."""
        logger.info("Gate Monitor Active. Waiting for commands or movement...")
        try:
            while True:
                self.process_incoming_commands()
                time.sleep(0.1)  
        except KeyboardInterrupt:
            logger.info("Manual shutdown initiated.")
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        """Ensures hardware resources are safely released."""
        logger.info("Cleaning up hardware resources...")
        if self.lora and self.lora.is_open:
            self.lora.close()
        if self.gate_sensor:
            self.gate_sensor.close()
        if self.gate_relay:
            self.gate_relay.close()
        logger.info("Cleanup complete.")

def main():
    load_dotenv()

    LORA_PORT = os.getenv("LORA_PORT", "/dev/serial0")
    LORA_BAUD = int(os.getenv("LORA_BAUD", 9600))
    SENSOR_GPIO = int(os.getenv("SENSOR_GPIO", 21))
    RELAY_GPIO = int(os.getenv("RELAY_GPIO", 17))

    monitor = RanchGateMonitor(
        serial_port=LORA_PORT,
        baud_rate=LORA_BAUD,
        sensor_pin=SENSOR_GPIO,
        relay_pin=RELAY_GPIO
    )

    try:
        monitor.setup()
        monitor.run()
    except Exception as e:
        logger.critical("Fatal error encountered. Service halted: %s", e)

if __name__ == "__main__":
    main()