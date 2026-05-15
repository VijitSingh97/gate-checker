import os
import time
import logging
import serial
import requests
from dotenv import load_dotenv

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class TelegramNotifier:
    """Handles communication with the Telegram Bot API."""
    
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    def send(self, message: str) -> None:
        """Sends a text message to the configured Telegram chat."""
        payload = {"chat_id": self.chat_id, "text": message}
        try:
            # Always include a timeout to prevent the application from hanging
            response = requests.post(self.base_url, data=payload, timeout=5)
            response.raise_for_status()
            logger.info("Telegram Alert Sent: %s", message)
        except requests.exceptions.RequestException as e:
            logger.error("Failed to send Telegram notification: %s", e)


class LoRaBaseStation:
    """Listens for incoming LoRa data and routes alerts."""

    def __init__(self, serial_port: str, baud_rate: int, notifier: TelegramNotifier):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.notifier = notifier
        self.lora = None

    def setup(self) -> None:
        """Initializes the serial connection."""
        logger.info("Initializing Base Station on %s at %d baud...", self.serial_port, self.baud_rate)
        try:
            self.lora = serial.Serial(self.serial_port, self.baud_rate, timeout=1)
            self.notifier.send("📡 Gate Monitor Base Station is Online.")
        except serial.SerialException as e:
            logger.error("Failed to connect to LoRa module: %s", e)
            raise

    def process_incoming_data(self) -> None:
        """Reads and routes incoming serial data."""
        if self.lora.in_waiting > 0:
            try:
                raw_data = self.lora.readline().decode('utf-8', errors='ignore').strip()
                
                if raw_data:
                    logger.info("LoRa Received: %s", raw_data)
                    self._route_message(raw_data)
            except Exception as e:
                logger.error("Error reading from serial port: %s", e)

    def _route_message(self, data: str) -> None:
        """Matches incoming strings to specific alert actions."""
        if "ALERT: Gate OPEN" in data:
            self.notifier.send("🚨 ALERT: The Ranch Gate is OPEN!")
        elif "STATUS: Gate Closed" in data:
            self.notifier.send("✅ Gate is now CLOSED.")
        # Future extensibility: Add battery low warnings, heartbeat ACKs, etc.

    def run(self) -> None:
        """Main execution loop."""
        logger.info("Receiver Online. Listening for Gate LoRa signals...")
        try:
            while True:
                self.process_incoming_data()
                time.sleep(0.1)  # Prevent CPU pegging
        except KeyboardInterrupt:
            logger.info("Manual shutdown initiated.")
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        """Safely closes hardware resources."""
        logger.info("Cleaning up resources...")
        if self.lora and self.lora.is_open:
            self.lora.close()
        logger.info("Shutdown complete.")


def main():
    load_dotenv()

    # --- Configuration Extraction ---
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    LORA_PORT = os.getenv("LORA_PORT", "/dev/serial0")
    LORA_BAUD = int(os.getenv("LORA_BAUD", 9600))

    if not TOKEN or not CHAT_ID:
        logger.critical("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID in environment variables. Halting.")
        return

    # --- Dependency Injection & Execution ---
    notifier = TelegramNotifier(token=TOKEN, chat_id=CHAT_ID)
    base_station = LoRaBaseStation(
        serial_port=LORA_PORT, 
        baud_rate=LORA_BAUD, 
        notifier=notifier
    )

    try:
        base_station.setup()
        base_station.run()
    except Exception as e:
        logger.critical("Fatal error encountered. Service halted: %s", e)

if __name__ == "__main__":
    main()