import os
import time
import logging
import serial
from gpiozero import Button
from dotenv import load_dotenv

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class RanchGateMonitor:
    """Monitors a physical gate via GPIO and transmits state changes over LoRa."""
    
    # Constants for LoRa Messages
    MSG_GATE_OPEN = "ALERT: Gate OPEN"
    MSG_GATE_CLOSED = "STATUS: Gate Closed"

    def __init__(self, serial_port: str, baud_rate: int, sensor_pin: int):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.sensor_pin = sensor_pin
        self.lora = None
        self.gate_sensor = None

    def setup(self) -> None:
        """Initializes hardware connections."""
        logger.info("Initializing LoRa connection on %s at %d baud...", self.serial_port, self.baud_rate)
        try:
            self.lora = serial.Serial(self.serial_port, self.baud_rate, timeout=1)
        except serial.SerialException as e:
            logger.error("Failed to connect to LoRa module: %s", e)
            raise

        logger.info("Initializing Gate Sensor on GPIO %d...", self.sensor_pin)
        self.gate_sensor = Button(self.sensor_pin, pull_up=True, bounce_time=0.2)
        
        # Register hardware callbacks
        self.gate_sensor.when_released = self.on_gate_opened
        self.gate_sensor.when_pressed = self.on_gate_closed

    def send_lora_message(self, message: str) -> None:
        """Transmits a message over the LoRa serial connection."""
        if self.lora and self.lora.is_open:
            try:
                logger.info("Transmitting: %s", message)
                self.lora.write(message.encode('utf-8'))
            except Exception as e:
                logger.error("Transmission failed: %s", e)
        else:
            logger.warning("LoRa serial connection is offline. Cannot send message.")

    def on_gate_opened(self) -> None:
        """Callback triggered when the magnetic circuit breaks."""
        logger.warning("Magnet separated! Gate is OPEN.")
        self.send_lora_message(self.MSG_GATE_OPEN)

    def on_gate_closed(self) -> None:
        """Callback triggered when the magnetic circuit is completed."""
        logger.info("Magnet detected! Gate is CLOSED.")
        self.send_lora_message(self.MSG_GATE_CLOSED)

    def run(self) -> None:
        """Main execution loop to keep the process alive."""
        logger.info("Gate Monitor Active. Waiting for movement...")
        try:
            # Keeps the main thread alive so background GPIO threads can run
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Manual shutdown initiated (KeyboardInterrupt).")
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        """Ensures hardware resources are safely released."""
        logger.info("Cleaning up hardware resources...")
        if self.lora and self.lora.is_open:
            self.lora.close()
        if self.gate_sensor:
            self.gate_sensor.close()
        logger.info("Cleanup complete.")

def main():
    # 1. Load environment variables from the .env file
    load_dotenv()

    # 2. Extract configuration with safe fallbacks
    LORA_PORT = os.getenv("LORA_PORT", "/dev/serial0")
    LORA_BAUD = int(os.getenv("LORA_BAUD", 9600))
    SENSOR_GPIO = int(os.getenv("SENSOR_GPIO", 21))

    # 3. Instantiate and run the monitor
    monitor = RanchGateMonitor(
        serial_port=LORA_PORT,
        baud_rate=LORA_BAUD,
        sensor_pin=SENSOR_GPIO
    )

    try:
        monitor.setup()
        monitor.run()
    except Exception as e:
        logger.critical("Fatal error encountered. Service halted: %s", e)

if __name__ == "__main__":
    main()