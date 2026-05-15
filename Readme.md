# 📡 LoRa Ranch Sentinel

A point-to-point LoRa monitoring system designed for remote ranch gates. This project bridges physical hardware sensors with real-time mobile notifications, operating independently of local Wi-Fi networks at the edge.

## 🏗️ Architecture
The system utilizes a decoupled, two-node architecture:
1. **The Edge Node (Gate Monitor):** A Raspberry Pi interfacing with a heavy-duty wide-gap magnetic contact sensor via GPIO. It detects state changes and broadcasts payload data over a 900MHz/433MHz LoRa serial connection.
2. **The Base Station (Receiver):** A secondary receiver located within internet range that listens for LoRa payloads and routes alerts via the Telegram Bot API using dependency-injected notification services.