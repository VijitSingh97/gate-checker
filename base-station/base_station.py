import os
import time
import logging
import serial
import requests
import sqlite3
from dotenv import load_dotenv

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class DatabaseLogger:
    """Handles local SQLite database logging for gate history."""
    
    def __init__(self, db_path="gate_history.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Creates the database and table if they don't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        event_type TEXT,
                        details TEXT
                    )
                ''')
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to initialize database: %s", e)

    def log(self, event_type: str, details: str):
        """Inserts a new record into the database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO logs (event_type, details) VALUES (?, ?)",
                    (event_type.upper(), details)
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to write to database: %s", e)

    def get_recent_logs(self, limit: int = 10, include_heartbeats: bool = False) -> list:
        """Retrieves recent logs, converting UTC timestamps to local Pi time."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if include_heartbeats:
                    cursor.execute(
                        "SELECT DATETIME(timestamp, 'localtime'), event_type, details FROM logs ORDER BY id DESC LIMIT ?", 
                        (limit,)
                    )
                else:
                    cursor.execute(
                        "SELECT DATETIME(timestamp, 'localtime'), event_type, details FROM logs WHERE event_type != 'HEARTBEAT' ORDER BY id DESC LIMIT ?", 
                        (limit,)
                    )
                return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error("Failed to read from database: %s", e)
            return []


class TelegramNotifier:
    """Handles bidirectional communication with the Telegram Bot API."""
    
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.send_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self.get_url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        self.last_update_id = 0

    def send(self, message: str, parse_mode: str = "HTML") -> None:
        """Sends a text message to the configured Telegram chat."""
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode}
        try:
            requests.post(self.send_url, data=payload, timeout=5)
            logger.info("Telegram Alert Sent.")
        except requests.exceptions.RequestException as e:
            logger.error("Failed to send Telegram notification: %s", e)

    def get_commands(self) -> list:
        """Polls Telegram for new user commands."""
        commands = []
        payload = {"offset": self.last_update_id + 1, "timeout": 2}
        try:
            response = requests.get(self.get_url, params=payload, timeout=5)
            if response.status_code == 200:
                data = response.json()
                for item in data.get("result", []):
                    self.last_update_id = item["update_id"]
                    text = item.get("message", {}).get("text", "").lower()
                    if text:
                        commands.append(text)
        except requests.exceptions.RequestException:
            pass 
        return commands


class LoRaBaseStation:
    """Listens for LoRa data, processes Telegram commands, and routes alerts."""

    def __init__(self, serial_port: str, baud_rate: int, notifier: TelegramNotifier, db: DatabaseLogger):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.notifier = notifier
        self.db = db
        self.lora = None
        
        # State Machine Tracking
        self.pending_action = None  
        self.waiting_for_ping = False
        self.ping_timeout = 0
        
        # Heartbeat Configuration
        self.heartbeat_interval = 300  
        self.last_heartbeat_time = time.time()
        self.gate_is_offline = False   

    def setup(self) -> None:
        """Initializes the serial connection."""
        logger.info("Initializing Base Station on %s at %d baud...", self.serial_port, self.baud_rate)
        try:
            self.lora = serial.Serial(self.serial_port, self.baud_rate, timeout=1)
            self.notifier.send("📡 Gate Monitor Base Station is Online.\nSend <b>/open</b> or <b>/close</b> to control.\nSend <b>/status</b> for history.")
            self.db.log("SYSTEM", "Base Station initialized and online.")
        except serial.SerialException as e:
            logger.error("Failed to connect to LoRa module: %s", e)
            self.db.log("ERROR", f"Serial connection failed: {e}")
            raise

    def send_lora(self, message: str) -> None:
        """Transmits a message via LoRa."""
        logger.info("Transmitting LoRa: %s", message)
        self.lora.write((message + "\n").encode('utf-8'))
        time.sleep(0.5) 

    def _get_emoji_for_event(self, event_type: str) -> str:
        """Maps event types to visually distinct emojis for the status report."""
        emojis = {
            "SYSTEM": "⚙️",
            "COMMAND": "👤",
            "HEARTBEAT": "💓",
            "ACTION_EXECUTED": "📡",
            "ACTION_SKIPPED": "🛑",
            "PHYSICAL_EVENT": "🚪",
            "ERROR": "⚠️",
            "CRITICAL": "🚨"
        }
        return emojis.get(event_type.upper(), "▪️")

    def process_telegram_commands(self) -> None:
        """Checks for Telegram commands and initiates the ping sequence."""
        commands = self.notifier.get_commands()
        for cmd in commands:
            if cmd == "/open" or cmd == "/close":
                desired_state = "open" if cmd == "/open" else "close"
                
                self.db.log("COMMAND", f"User requested to {desired_state} the gate via Telegram.")
                self.notifier.send(f"⏳ Pinging gate to verify status before attempting to {desired_state}...")
                
                self.pending_action = desired_state
                self.waiting_for_ping = True
                self.ping_timeout = time.time() + 15 
                
                self.send_lora("CMD:STATUS_REQ")
                
            elif cmd.startswith("/status"):
                include_heartbeats = "all" in cmd
                # Fetch more records if they want the full dump, fewer for the clean view
                limit = 15 if include_heartbeats else 10 
                
                logs = self.db.get_recent_logs(limit=limit, include_heartbeats=include_heartbeats)
                
                if not logs:
                    self.notifier.send("📭 No logs found in the database.")
                    continue
                
                # Build the formatted message
                header = "📋 <b>Complete Gate History</b>\n" if include_heartbeats else "📋 <b>Recent Gate Activity</b>\n"
                msg_lines = [header]
                
                for timestamp, event_type, details in logs:
                    emoji = self._get_emoji_for_event(event_type)
                    # Strip the seconds off the timestamp for a cleaner look (e.g., 2026-05-15 18:52)
                    short_time = timestamp[:-3] if timestamp else "Unknown Time"
                    
                    msg_lines.append(f"{emoji} <b>{short_time}</b>\n└ <i>{details}</i>")
                    
                # Join the lines and send
                final_message = "\n".join(msg_lines)
                self.notifier.send(final_message)

    def process_incoming_lora(self) -> None:
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
        current_gate_state = None

        if "OPEN" in data.upper(): 
            current_gate_state = "open"
        elif "CLOSED" in data.upper(): 
            current_gate_state = "close"

        if current_gate_state:
            # Reconnection Logic
            if self.gate_is_offline:
                self.gate_is_offline = False
                self.db.log("SYSTEM", "Gate monitor reconnected.")
                self.notifier.send("✅ Gate Monitor has reconnected and is back online!")

            # Handling Ping Responses
            if self.waiting_for_ping:
                self.waiting_for_ping = False
                
                if self.pending_action == 'heartbeat':
                    logger.info("Heartbeat ACK received. Gate is %s.", current_gate_state)
                    self.db.log("HEARTBEAT", f"ACK received. Gate is {current_gate_state}.")
                    
                elif current_gate_state == self.pending_action:
                    self.db.log("ACTION_SKIPPED", f"Gate is already {current_gate_state}.")
                    self.notifier.send(f"🛑 Gate is already {current_gate_state.upper()}. No action taken.")
                    
                else:
                    self.db.log("ACTION_EXECUTED", f"Triggering remote to {self.pending_action}.")
                    self.notifier.send(f"🔄 Gate is {current_gate_state.upper()}. Triggering remote to {self.pending_action}...")
                    self.send_lora(f"CMD:{self.pending_action.upper()}")
                
                self.pending_action = None 
            
            # Handling Spontaneous Physical Events
            else:
                self.db.log("PHYSICAL_EVENT", f"Gate physically moved to {current_gate_state}.")
                if current_gate_state == 'open':
                    self.notifier.send("🚨 <b>ALERT:</b> The Ranch Gate is OPEN!")
                elif current_gate_state == 'close':
                    self.notifier.send("✅ Gate is now CLOSED.")

    def run(self) -> None:
        """Main execution loop."""
        logger.info("Receiver Online. Listening for Gate LoRa signals and Telegram commands...")
        try:
            while True:
                self.process_incoming_lora()
                self.process_telegram_commands()
                
                current_time = time.time()
                
                # Timeout Handling
                if self.waiting_for_ping and current_time > self.ping_timeout:
                    self.waiting_for_ping = False
                    
                    if self.pending_action == 'heartbeat':
                        if not self.gate_is_offline:
                            self.gate_is_offline = True
                            self.db.log("ERROR", "Heartbeat failed. Gate is offline.")
                            self.notifier.send("⚠️ <b>HEARTBEAT FAILED:</b> The gate monitor is offline or out of range.")
                    else:
                        self.db.log("ERROR", "Command ping timed out.")
                        self.notifier.send("⚠️ <b>ERROR:</b> Gate did not respond to status ping. It may be offline.")
                        
                    self.pending_action = None
                
                # Trigger Heartbeat
                if not self.waiting_for_ping and (current_time - self.last_heartbeat_time > self.heartbeat_interval):
                    logger.info("Sending routine heartbeat ping...")
                    self.pending_action = 'heartbeat'
                    self.waiting_for_ping = True
                    self.ping_timeout = current_time + 15
                    self.last_heartbeat_time = current_time
                    self.send_lora("CMD:STATUS_REQ")
                    
                time.sleep(0.5) 
                
        except KeyboardInterrupt:
            logger.info("Manual shutdown initiated.")
            self.db.log("SYSTEM", "Manual shutdown initiated.")
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

    TOKEN = os.getenv("TELEGRAM_TOKEN")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    LORA_PORT = os.getenv("LORA_PORT", "/dev/serial0")
    LORA_BAUD = int(os.getenv("LORA_BAUD", 9600))

    if not TOKEN or not CHAT_ID:
        logger.critical("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID in environment variables. Halting.")
        return

    # Dependency Injection
    db = DatabaseLogger("gate_history.db")
    notifier = TelegramNotifier(token=TOKEN, chat_id=CHAT_ID)
    base_station = LoRaBaseStation(serial_port=LORA_PORT, baud_rate=LORA_BAUD, notifier=notifier, db=db)

    try:
        base_station.setup()
        base_station.run()
    except Exception as e:
        logger.critical("Fatal error encountered. Service halted: %s", e)
        db.log("CRITICAL", f"Service crashed: {e}")

if __name__ == "__main__":
    main()