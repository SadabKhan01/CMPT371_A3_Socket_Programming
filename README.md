# SocketShare

**Course:** CMPT 371 - Data Communications & Networking  
**Submission note:** Only one student should submit the final GitHub repository link.

SocketShare is a TCP client-server file transfer application built with Python sockets. It demonstrates connection establishment, data exchange, integrity verification with SHA-256, multiple client handling with threads, and clean connection termination in a way that is easy to run, understand, and demo for a university networking assignment.

## 1. Project Title

SocketShare: TCP Client-Server File Transfer System

## 2. Short Description

This project implements a reliable file transfer application using the Python Socket API and a client-server architecture. The server accepts multiple clients concurrently, stores uploaded files, lists files available on the server, and sends requested files back to clients. The client provides a simple command-line menu so the workflow is easy to follow during testing and grading.

## 3. Architecture Overview

- **Architecture type:** Client-server
- **Transport protocol:** TCP
- **Concurrency model:** One server thread per connected client
- **Application protocol:** Length-prefixed JSON headers for control messages, followed by raw file bytes for upload and download data

### How the architecture works

1. The server starts on a configurable host and port and waits for incoming TCP connections.
2. Each client connects to the server over TCP.
3. The client sends a JSON control message such as `LIST`, `UPLOAD`, `DOWNLOAD`, or `QUIT`.
4. For file transfers, the sender first shares metadata including filename, file size, and SHA-256 hash.
5. The receiver reads exactly the announced number of bytes and then verifies integrity using SHA-256.
6. The client or server reports success, failure, or disconnect events clearly to the terminal.

## 4. Features

- TCP socket-based client-server communication
- Multiple clients handled concurrently using `threading`
- Interactive client CLI menu
- `LIST` server files
- `UPLOAD` local file to server storage
- `DOWNLOAD` server file to local `downloads/`
- `HELP` command for usability
- `QUIT` for clean disconnection
- Download selection by file number or exact filename
- SHA-256 integrity verification for uploads and downloads
- Graceful handling of common errors such as missing files, invalid commands, disconnects, and port conflicts
- Simple duplicate filename protection by auto-renaming files instead of overwriting them
- Timestamped server logging for easier demos and debugging

## 5. File Structure

```text
CMPT371_A3_Socket_Programming/
├── README.md
├── .gitignore
├── demo/
│   └── A3_Demo.mov
├── requirements.txt
├── downloads/
├── sample_files/
│   └── demo.txt
├── src/
│   ├── client.py
│   ├── config.py
│   ├── protocol.py
│   ├── server.py
│   └── utils.py
└── storage/
    └── uploads/
```

## 6. Requirements / Python Version

- Python 3.9 or newer
- No external Python packages are required
- Uses only the Python standard library

## 7. Fresh Environment Setup

These instructions assume a fresh machine after cloning the repository.

### Step 1: Open a terminal and move into the project folder

```bash
cd CMPT371_A3_Socket_Programming
```

### Step 2: Confirm Python is installed

```bash
python3 --version
```

Expected result: Python 3.9+ is required. Python 3.11+ is recommended.

### Step 3: Create a virtual environment (recommended)

```bash
python3 -m venv .venv
```

### Step 4: Activate the virtual environment

On macOS/Linux:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### Step 5: Install requirements

```bash
pip install -r requirements.txt
```

`requirements.txt` is intentionally minimal because this project uses only the Python standard library.

## 8. Exact Command to Run the Server

From inside `CMPT371_A3_Socket_Programming/`:

```bash
python3 src/server.py
```

Optional custom host/port:

```bash
python3 src/server.py --host 127.0.0.1 --port 5001
```

When the server starts successfully, it prints the listening address and upload directory.

## 9. Exact Command to Run the Client

Open a second terminal, move into the same project folder, and run:

```bash
cd CMPT371_A3_Socket_Programming
python3 src/client.py
```

Optional custom host/port:

```bash
python3 src/client.py --host 127.0.0.1 --port 5001
```

## 10. Example Usage Workflow

### Start the server

```bash
python3 src/server.py
```

### Start one client

```bash
python3 src/client.py
```

### Try these actions in the client menu

1. Choose `1` to list files on the server.
2. Choose `2` and upload `sample_files/demo.txt`.
3. Choose `1` again to confirm the file now appears on the server.
4. Choose `3` and download the file by entering either `1` or `demo.txt`.
5. Choose `5` to quit cleanly.

## 11. Example Commands for One Server and Two Clients

Use three terminals for a quick concurrency demo.

### Terminal 1: Server

```bash
cd CMPT371_A3_Socket_Programming
python3 src/server.py
```

### Terminal 2: Client A

```bash
cd CMPT371_A3_Socket_Programming
python3 src/client.py
```

### Terminal 3: Client B

```bash
cd CMPT371_A3_Socket_Programming
python3 src/client.py
```

You can then:

- use Client A to upload `sample_files/demo.txt`
- use Client B to run `LIST`
- use Client B to download the uploaded file
- quit both clients cleanly

This demonstrates multiple clients connected to the same server at the same time.

## 12. Demo Walkthrough for a 2-Minute Video

This sequence is short and works well for the assignment demo:

1. Show `python3 src/server.py` starting successfully.
2. Open Client A and show the main menu.
3. Run `LIST` and show that the server is initially empty or has existing files.
4. Upload `sample_files/demo.txt`.
5. Show the server logs confirming the upload.
6. Open Client B and run `LIST` to show the shared file on the server.
7. Download `demo.txt` using Client B.
8. Show the SHA-256 integrity verification message.
9. Quit both clients using `QUIT`.
10. Stop the server with `Ctrl+C` to show graceful shutdown.

## 13. Limitations / Issues / Assumptions

- This project uses plain TCP sockets with no authentication or encryption.
- It is designed for local or small-scale educational testing, not production deployment.
- Files are transferred one at a time per client connection.
- There is no resume support for interrupted transfers.
- The server stores files in a local folder and does not use a database.
- Only filenames are supported; directory upload/download is not implemented.
- Very large files are not specifically optimized beyond chunked transfer.
- The thread-per-client model is simple and easy to understand, but it is not intended for very high numbers of simultaneous users.

## 14. Team Member Section

Fill this section before submission.

| Name | Student ID | Email |
| --- | --- | --- |
| Your Name Here | Your Student ID Here | your_email@example.com |
| Partner Name Here | Partner Student ID Here | partner_email@example.com |

## 15. Video Demo Link

The repository now includes the demo video file directly:

- Demo video file: [A3_Demo.mov](demo/A3_Demo.mov)
- Reminder: keep the final recording under 2 minutes to match the rubric

## 16. Troubleshooting

### Problem: `Address already in use`

Another program is already using the same port.

Solution:

- close the old server process
- or start the server with a different port, for example:

```bash
python3 src/server.py --port 5002
```

### Problem: Client cannot connect

Possible causes:

- the server is not running
- the host or port does not match the server

Solution:

- start the server first
- confirm both sides use the same `--host` and `--port`

### Problem: Local file not found during upload

Solution:

- check the path you typed in the client
- use `sample_files/demo.txt` for a quick verified test

### Problem: File not found on the server during download

Solution:

- run `LIST` first
- make sure the filename matches exactly

### Problem: Integrity verification failed

Solution:

- retry the transfer
- ensure the connection was not interrupted during upload or download
