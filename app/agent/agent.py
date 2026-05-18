import os
import sys
import time
import json
import psutil
import subprocess
import threading
import traceback
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Pre-import flask & smolagents elements
from flask import Flask, jsonify, render_template_string, request
from smolagents import CodeAgent, DuckDuckGoSearchTool, HfApiModel, OpenAiApiModel, Tool

# Resolve absolute paths relative to this script
# agent.py is at app/agent/agent.py, so parent of parent is root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.json")
ACTIVITY_LOG = os.path.join(BASE_DIR, "activity.log")
ERROR_LOG = os.path.join(BASE_DIR, "error.log")
SCRIPT_TO_RUN = os.path.join(BASE_DIR, "ali_msg 1.py")

# Global variables to track the background process
ali_process = None
ali_process_lock = threading.Lock()
restart_count = 0
start_time = datetime.now()

# Initialize Flask app
app = Flask(__name__)

# ------------------ AGENTIC AI TOOLS ------------------

class SessionCookiesManagerTool(Tool):
    name = "session_cookies_checker"
    description = "Checks the presence, validity, and count of Alibaba session cookies inside cookies.json."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        if not os.path.exists(COOKIES_FILE):
            return f"❌ cookies.json does not exist at {COOKIES_FILE}. Alibaba session will require manual login."
        
        try:
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            
            if not isinstance(cookies, list):
                return "❌ cookies.json is present but format is invalid (expected a JSON array)."
            
            num_cookies = len(cookies)
            domains = set(c.get("domain", "") for c in cookies if c.get("domain"))
            
            # Simple expiry check
            now = time.time()
            expired_count = 0
            for c in cookies:
                expiry = c.get("expiry") or c.get("expirationDate")
                if expiry and float(expiry) < now:
                    expired_count += 1
            
            return (
                f"✅ cookies.json is present.\n"
                f"- Total cookies found: {num_cookies}\n"
                f"- Covered domains: {', '.join(domains)}\n"
                f"- Expired cookies in session: {expired_count} / {num_cookies}\n"
                f"- Status: {'⚠️ Needs refresh' if expired_count > num_cookies * 0.7 else '🟢 Generally Active'}"
            )
        except Exception as e:
            return f"❌ Error reading cookies.json: {str(e)}"

class AlibabaLogAnalyzerTool(Tool):
    name = "alibaba_log_analyzer"
    description = "Reads and analyzes the latest N lines of activity.log and error.log to detect unread messages, warnings, webhooks, or failures."
    inputs = {
        "num_lines": {"type": "integer", "description": "Number of recent log lines to inspect (default 50)", "nullable": True}
    }
    output_type = "string"

    def forward(self, num_lines: int = 50) -> str:
        lines_to_read = num_lines or 50
        summary = []
        
        # Read activity log
        if os.path.exists(ACTIVITY_LOG):
            summary.append("--- Last Activity Logs ---")
            try:
                with open(ACTIVITY_LOG, "r", encoding="utf-8", errors="ignore") as f:
                    act_lines = f.readlines()
                recent_act = [line.strip() for line in act_lines[-lines_to_read:]]
                summary.extend(recent_act)
            except Exception as e:
                summary.append(f"Error reading activity.log: {str(e)}")
        else:
            summary.append("⚠️ activity.log does not exist yet.")

        summary.append("\n")

        # Read error log
        if os.path.exists(ERROR_LOG):
            summary.append("--- Last Error Logs ---")
            try:
                with open(ERROR_LOG, "r", encoding="utf-8", errors="ignore") as f:
                    err_lines = f.readlines()
                recent_err = [line.strip() for line in err_lines[-lines_to_read:]]
                summary.extend(recent_err if recent_err else ["No recent errors log."])
            except Exception as e:
                summary.append(f"Error reading error.log: {str(e)}")
        else:
            summary.append("🟢 No error.log file (clean execution).")

        return "\n".join(summary)

class SystemDiagnosticsTool(Tool):
    name = "system_diagnostics"
    description = "Checks the system diagnostics: CPU load, RAM availability, disk space, and details of any active Chrome or Python driver processes."
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Check Chrome & Python processes
        chrome_count = 0
        python_count = 0
        for proc in psutil.process_iter(['name']):
            try:
                name = proc.info['name'].lower()
                if 'chrome' in name:
                    chrome_count += 1
                elif 'python' in name:
                    python_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        global ali_process
        runner_alive = "🔴 Terminated"
        if ali_process and ali_process.poll() is None:
            runner_alive = f"🟢 Running (PID: {ali_process.pid})"

        return (
            f"--- System Diagnostics ---\n"
            f"- CPU Usage: {cpu}%\n"
            f"- RAM Usage: {mem.percent}% ({mem.available // (1024 * 1024)}MB free)\n"
            f"- Disk Usage: {disk.percent}% ({disk.free // (1024 * 1024 * 1024)}GB free)\n"
            f"- Chrome Processes in System: {chrome_count}\n"
            f"- Python Processes: {python_count}\n"
            f"- background Automation Process: {runner_alive}\n"
            f"- Container Uptime: {str(datetime.now() - start_time).split('.')[0]}"
        )

# ------------------ INITIALIZE AGENT ------------------

def get_agent_model():
    openai_key = os.getenv("OPENAI_API_KEY")
    hf_token = os.getenv("HF_TOKEN")
    
    if openai_key:
        try:
            return OpenAiApiModel(model_id="gpt-4o-mini", api_key=openai_key)
        except Exception:
            pass
            
    # Default serverless API
    model_id = os.getenv("MODEL_ID", "Qwen/Qwen2.5-Coder-32B-Instruct")
    return HfApiModel(model_id=model_id, token=hf_token)

def run_agent_task(prompt: str) -> str:
    try:
        model = get_agent_model()
        cookies_tool = SessionCookiesManagerTool()
        log_tool = AlibabaLogAnalyzerTool()
        sys_tool = SystemDiagnosticsTool()
        search_tool = DuckDuckGoSearchTool()

        agent = CodeAgent(
            tools=[search_tool, cookies_tool, log_tool, sys_tool],
            model=model,
            add_base_tools=True
        )
        
        result = agent.run(prompt)
        return str(result)
    except Exception as e:
        return f"Agent error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"

# ------------------ BACKGROUND AUTOMATION WRAPPER ------------------

def log_system_activity(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(ACTIVITY_LOG, "a", encoding="utf-8") as log:
        log.write(f"{timestamp} - [AGENT_MANAGER] {msg}\n")
    print(f"📘 [AGENT_MANAGER] {msg}")

def start_alibaba_automation():
    global ali_process, restart_count
    with ali_process_lock:
        if ali_process and ali_process.poll() is None:
            log_system_activity("Alibaba automation is already running.")
            return True
        
        log_system_activity(f"Spawning background Alibaba automation process: {SCRIPT_TO_RUN}...")
        try:
            # Run the Python script as a subprocess
            # Pipe outputs so we can log them or inspect if needed
            ali_process = subprocess.Popen(
                [sys.executable, SCRIPT_TO_RUN],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=BASE_DIR,
                bufsize=1
            )
            
            # Start background thread to capture stdout/stderr and redirect to activity.log
            threading.Thread(target=stream_process_output, args=(ali_process,), daemon=True).start()
            
            log_system_activity(f"Successfully spawned ali_msg 1.py (PID: {ali_process.pid})")
            return True
        except Exception as e:
            log_system_activity(f"❌ Failed to spawn process: {str(e)}")
            return False

def stream_process_output(process):
    global restart_count
    for line in iter(process.stdout.readline, ''):
        # Print to console
        sys.stdout.write(line)
        sys.stdout.flush()
        # The lines are already logged inside ali_msg 1.py's log functions,
        # but in case it prints something raw (e.g. package installations or stack traces),
        # we log it to our activity.log
        if "Installing" in line or "error" in line.lower() or "exception" in line.lower() or "traceback" in line.lower():
            with open(ACTIVITY_LOG, "a", encoding="utf-8") as log:
                log.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [SUBPROCESS] {line}")
                
    process.stdout.close()
    return_code = process.wait()
    log_system_activity(f"⚠️ Subprocess (ali_msg 1.py) exited with return code: {return_code}")
    
    # Auto-restart logic if exited unexpectedly
    # Check if this thread belongs to the current active process
    with ali_process_lock:
        if ali_process == process:
            restart_count += 1
            log_system_activity(f"Restarting subprocess (Count: {restart_count})...")
            threading.Thread(target=lambda: (time.sleep(10), start_alibaba_automation()), daemon=True).start()

def stop_alibaba_automation():
    global ali_process
    with ali_process_lock:
        if ali_process:
            log_system_activity(f"Terminating subprocess PID {ali_process.pid}...")
            try:
                # Terminate subprocess
                ali_process.terminate()
                try:
                    ali_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ali_process.kill()
                log_system_activity("Subprocess terminated.")
            except Exception as e:
                log_system_activity(f"Error terminating subprocess: {str(e)}")
            ali_process = None
            
        # Clean up any residual chrome processes on linux/windows
        try:
            if sys.platform == "win32":
                os.system("taskkill /F /IM chrome.exe /T >nul 2>&1")
                os.system("taskkill /F /IM chromedriver.exe /T >nul 2>&1")
            else:
                os.system("pkill -9 -f chrome >/dev/null 2>&1")
                os.system("pkill -9 -f chromedriver >/dev/null 2>&1")
            log_system_activity("Cleaned up orphaned Chrome processes.")
        except Exception:
            pass

# ------------------ FLASK ROUTES & DASHBOARD ------------------

@app.route('/')
def dashboard():
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Alibaba Agentic AI Dashboard</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg: #0b0f19;
                --card-bg: rgba(17, 24, 39, 0.7);
                --card-border: rgba(255, 255, 255, 0.06);
                --text: #f3f4f6;
                --text-muted: #9ca3af;
                --accent: #8b5cf6;
                --accent-glow: rgba(139, 92, 246, 0.3);
                --success: #10b981;
                --success-glow: rgba(16, 185, 129, 0.2);
                --error: #ef4444;
            }

            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }

            body {
                font-family: 'Outfit', sans-serif;
                background-color: var(--bg);
                color: var(--text);
                background-image: 
                    radial-gradient(at 10% 20%, rgba(139, 92, 246, 0.15) 0px, transparent 50%),
                    radial-gradient(at 90% 80%, rgba(59, 130, 246, 0.15) 0px, transparent 50%);
                background-attachment: fixed;
                min-height: 100vh;
                padding: 2rem;
            }

            .container {
                max-width: 1400px;
                margin: 0 auto;
                display: flex;
                flex-direction: column;
                gap: 2rem;
            }

            /* Glassmorphic Header */
            header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 1.5rem 2rem;
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                border-radius: 20px;
                backdrop-filter: blur(16px);
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            }

            .logo-group h1 {
                font-size: 1.8rem;
                font-weight: 800;
                background: linear-gradient(135deg, #a78bfa, #3b82f6);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                letter-spacing: 1px;
            }

            .logo-group p {
                font-size: 0.9rem;
                color: var(--text-muted);
                margin-top: 0.2rem;
            }

            .header-controls {
                display: flex;
                align-items: center;
                gap: 1.5rem;
            }

            .status-badge {
                display: flex;
                align-items: center;
                gap: 0.6rem;
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid var(--card-border);
                padding: 0.6rem 1.2rem;
                border-radius: 50px;
                font-weight: 600;
                font-size: 0.95rem;
            }

            .status-dot {
                width: 10px;
                height: 10px;
                border-radius: 50%;
                background: var(--error);
                box-shadow: 0 0 10px var(--error);
            }

            .status-dot.active {
                background: var(--success);
                box-shadow: 0 0 10px var(--success);
                animation: pulse 2s infinite;
            }

            .btn {
                font-family: inherit;
                cursor: pointer;
                border: none;
                font-weight: 600;
                padding: 0.7rem 1.5rem;
                border-radius: 12px;
                transition: all 0.3s ease;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 0.5rem;
            }

            .btn-primary {
                background: linear-gradient(135deg, var(--accent), #6d28d9);
                color: white;
                box-shadow: 0 4px 15px var(--accent-glow);
            }

            .btn-primary:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(139, 92, 246, 0.5);
            }

            .btn-danger {
                background: rgba(239, 68, 68, 0.2);
                border: 1px solid rgba(239, 68, 68, 0.4);
                color: #fca5a5;
            }

            .btn-danger:hover {
                background: rgba(239, 68, 68, 0.4);
                transform: translateY(-2px);
            }

            /* Dashboard Grid */
            .grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 2rem;
            }

            @media (max-width: 1024px) {
                .grid {
                    grid-template-columns: 1fr;
                }
            }

            /* Glassmorphic Card */
            .card {
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                border-radius: 24px;
                padding: 2rem;
                backdrop-filter: blur(16px);
                box-shadow: 0 15px 35px rgba(0, 0, 0, 0.4);
                display: flex;
                flex-direction: column;
                gap: 1.5rem;
            }

            .card-title {
                font-size: 1.3rem;
                font-weight: 700;
                display: flex;
                align-items: center;
                gap: 0.8rem;
                border-bottom: 1px solid rgba(255, 255, 255, 0.05);
                padding-bottom: 0.8rem;
            }

            /* System Stats Section */
            .stats-grid {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 1.2rem;
            }

            .stat-box {
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--card-border);
                border-radius: 16px;
                padding: 1.2rem;
                display: flex;
                flex-direction: column;
                gap: 0.4rem;
            }

            .stat-label {
                font-size: 0.85rem;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            .stat-value {
                font-size: 1.5rem;
                font-weight: 700;
            }

            /* Progress Bar */
            .progress-container {
                width: 100%;
                background: rgba(255, 255, 255, 0.05);
                height: 6px;
                border-radius: 10px;
                overflow: hidden;
                margin-top: 0.3rem;
            }

            .progress-bar {
                height: 100%;
                background: linear-gradient(90deg, var(--accent), #3b82f6);
                border-radius: 10px;
                transition: width 0.8s ease;
            }

            /* Terminal View */
            .terminal {
                background: #060913;
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 16px;
                padding: 1.2rem;
                font-family: 'Courier New', Courier, monospace;
                font-size: 0.85rem;
                color: #34d399;
                height: 380px;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 0.4rem;
                white-space: pre-wrap;
            }

            /* Agent Console Section */
            .agent-console {
                display: flex;
                flex-direction: column;
                gap: 1rem;
                height: 480px;
            }

            .chat-output {
                flex-grow: 1;
                background: rgba(0, 0, 0, 0.2);
                border: 1px solid rgba(255, 255, 255, 0.04);
                border-radius: 16px;
                padding: 1.2rem;
                overflow-y: auto;
                display: flex;
                flex-direction: column;
                gap: 1rem;
                font-size: 0.95rem;
                line-height: 1.5;
            }

            .chat-msg {
                padding: 0.8rem 1.2rem;
                border-radius: 16px;
                max-width: 85%;
            }

            .chat-msg.user {
                background: rgba(139, 92, 246, 0.15);
                border: 1px solid rgba(139, 92, 246, 0.3);
                align-self: flex-end;
                color: #e9d5ff;
            }

            .chat-msg.agent {
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid var(--card-border);
                align-self: flex-start;
                white-space: pre-wrap;
            }

            .quick-actions {
                display: flex;
                flex-wrap: wrap;
                gap: 0.5rem;
                margin-top: 0.5rem;
            }

            .action-tag {
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid var(--card-border);
                color: var(--text-muted);
                padding: 0.4rem 0.8rem;
                border-radius: 8px;
                font-size: 0.8rem;
                cursor: pointer;
                font-weight: 600;
                transition: all 0.2s ease;
            }

            .action-tag:hover {
                background: rgba(139, 92, 246, 0.1);
                border-color: var(--accent);
                color: var(--text);
            }

            .input-group {
                display: flex;
                gap: 0.8rem;
            }

            .chat-input {
                flex-grow: 1;
                background: rgba(0, 0, 0, 0.3);
                border: 1px solid var(--card-border);
                border-radius: 12px;
                padding: 0.8rem 1.2rem;
                color: var(--text);
                font-family: inherit;
                font-size: 0.95rem;
                outline: none;
                transition: border 0.3s ease;
            }

            .chat-input:focus {
                border-color: var(--accent);
                box-shadow: 0 0 10px rgba(139, 92, 246, 0.2);
            }

            @keyframes pulse {
                0% {
                    box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.6);
                }
                70% {
                    box-shadow: 0 0 0 8px rgba(16, 185, 129, 0);
                }
                100% {
                    box-shadow: 0 0 0 0 rgba(16, 185, 129, 0);
                }
            }

            .pulse-load {
                animation: textPulse 1.5s infinite ease-in-out;
                color: var(--text-muted);
                font-style: italic;
            }

            @keyframes textPulse {
                0%, 100% { opacity: 0.4; }
                50% { opacity: 1; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <div class="logo-group">
                    <h1>ANTIGRAVITY AUTOMATION ENGINE</h1>
                    <p>Agentic AI Framework & Alibaba Autoresponder Dashboard</p>
                </div>
                <div class="header-controls">
                    <div class="status-badge">
                        <div id="statusDot" class="status-dot"></div>
                        <span id="statusText">Checking...</span>
                    </div>
                    <button class="btn btn-danger" onclick="restartAutomation()">🔄 Restart Autoresponder</button>
                </div>
            </header>

            <div class="grid">
                <!-- Left Side: Diagnostics and Logs -->
                <div style="display: flex; flex-direction: column; gap: 2rem;">
                    <div class="card">
                        <div class="card-title">
                            📊 Engine Status & Diagnostics
                        </div>
                        <div class="stats-grid">
                            <div class="stat-box">
                                <span class="stat-label">CPU LOAD</span>
                                <span id="cpuVal" class="stat-value">0%</span>
                                <div class="progress-container">
                                    <div id="cpuBar" class="progress-bar" style="width: 0%;"></div>
                                </div>
                            </div>
                            <div class="stat-box">
                                <span class="stat-label">RAM USAGE</span>
                                <span id="memVal" class="stat-value">0%</span>
                                <div class="progress-container">
                                    <div id="memBar" class="progress-bar" style="width: 0%;"></div>
                                </div>
                            </div>
                            <div class="stat-box">
                                <span class="stat-label">Chrome Handles</span>
                                <span id="chromeVal" class="stat-value">0 Active</span>
                            </div>
                            <div class="stat-box">
                                <span class="stat-label">Restarts / Uptime</span>
                                <span id="uptimeVal" class="stat-value" style="font-size:1.15rem; font-weight:600;">0 (0:00:00)</span>
                            </div>
                        </div>
                    </div>

                    <div class="card">
                        <div class="card-title">
                            💾 Live Automation Console Logs
                        </div>
                        <div id="logsTerminal" class="terminal">Loading logs...</div>
                    </div>
                </div>

                <!-- Right Side: Agent AI Console -->
                <div class="card">
                    <div class="card-title">
                        🧠 Agentic AI Console (smolagents)
                    </div>
                    <div class="agent-console">
                        <div id="chatOutput" class="chat-output">
                            <div class="chat-msg agent">Hello! I am your Antigravity AI Agent powered by the smolagents framework. I have direct access to your logs, cookies, and system diagnostics tools. Ask me anything about the system health, cookies status, or to perform web searches.</div>
                        </div>
                        <div class="quick-actions">
                            <span class="action-tag" onclick="quickPrompt('System diagnostics check')">📊 Diagnostic Check</span>
                            <span class="action-tag" onclick="quickPrompt('Check Alibaba cookies status')">🍪 Cookies Check</span>
                            <span class="action-tag" onclick="quickPrompt('Summarize recent activity logs')">📋 Activity Summary</span>
                        </div>
                        <div class="input-group">
                            <input type="text" id="chatInput" class="chat-input" placeholder="Ask your Agent a question (e.g. Check if cookies are expired)..." onkeydown="if(event.key === 'Enter') sendPrompt()">
                            <button class="btn btn-primary" onclick="sendPrompt()">Send Prompt</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            // Periodic status update
            function updateStatus() {
                fetch('/api/status')
                    .then(res => res.json())
                    .then(data => {
                        // Update status badge
                        const dot = document.getElementById('statusDot');
                        const statusTxt = document.getElementById('statusText');
                        if (data.runner_alive) {
                            dot.classList.add('active');
                            statusTxt.textContent = 'Active (PID: ' + data.runner_pid + ')';
                        } else {
                            dot.classList.remove('active');
                            statusTxt.textContent = 'Terminated';
                        }

                        // Update stats
                        document.getElementById('cpuVal').textContent = data.cpu + '%';
                        document.getElementById('cpuBar').style.width = data.cpu + '%';
                        
                        document.getElementById('memVal').textContent = data.memory + '%';
                        document.getElementById('memBar').style.width = data.memory + '%';

                        document.getElementById('chromeVal').textContent = data.chrome_processes + ' active';
                        document.getElementById('uptimeVal').textContent = data.restart_count + ' / ' + data.uptime;

                        // Update terminal logs
                        const term = document.getElementById('logsTerminal');
                        term.textContent = data.logs.join('\\n');
                        term.scrollTop = term.scrollHeight;
                    })
                    .catch(err => console.error('Error fetching status:', err));
            }

            function restartAutomation() {
                if (confirm('Are you sure you want to restart the Alibaba Autoresponder process?')) {
                    fetch('/api/restart', { method: 'POST' })
                        .then(res => res.json())
                        .then(data => {
                            alert(data.message);
                            updateStatus();
                        });
                }
            }

            function quickPrompt(text) {
                document.getElementById('chatInput').value = text;
                sendPrompt();
            }

            function sendPrompt() {
                const input = document.getElementById('chatInput');
                const prompt = input.value.trim();
                if (!prompt) return;

                input.value = '';
                
                // Add user message to chat
                const chat = document.getElementById('chatOutput');
                const userMsg = document.createElement('div');
                userMsg.className = 'chat-msg user';
                userMsg.textContent = prompt;
                chat.appendChild(userMsg);
                chat.scrollTop = chat.scrollHeight;

                // Add loading indicator
                const loader = document.createElement('div');
                loader.className = 'chat-msg agent pulse-load';
                loader.id = 'tempLoader';
                loader.textContent = 'Agent is thinking and executing tools...';
                chat.appendChild(loader);
                chat.scrollTop = chat.scrollHeight;

                fetch('/api/agent', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt: prompt })
                })
                .then(res => res.json())
                .then(data => {
                    document.getElementById('tempLoader').remove();
                    const agentMsg = document.createElement('div');
                    agentMsg.className = 'chat-msg agent';
                    agentMsg.textContent = data.result;
                    chat.appendChild(agentMsg);
                    chat.scrollTop = chat.scrollHeight;
                })
                .catch(err => {
                    document.getElementById('tempLoader').remove();
                    const errMsg = document.createElement('div');
                    errMsg.className = 'chat-msg agent';
                    errMsg.style.color = 'var(--error)';
                    errMsg.textContent = 'Error calling agent API: ' + err;
                    chat.appendChild(errMsg);
                    chat.scrollTop = chat.scrollHeight;
                });
            }

            // Initial and interval updates
            updateStatus();
            setInterval(updateStatus, 3000);
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)

@app.route('/api/status')
def api_status():
    global ali_process, restart_count
    
    # Calculate CPU/RAM
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory().percent
    
    chrome_count = 0
    for proc in psutil.process_iter(['name']):
        try:
            if 'chrome' in proc.info['name'].lower():
                chrome_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Get log lines
    logs = []
    if os.path.exists(ACTIVITY_LOG):
        try:
            with open(ACTIVITY_LOG, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            logs = [line.strip() for line in lines[-25:]]
        except Exception as e:
            logs = [f"Error reading activity.log: {str(e)}"]
    else:
        logs = ["Welcome! Waiting for activity.log to be initialized by the automation..."]

    runner_alive = False
    runner_pid = None
    if ali_process and ali_process.poll() is None:
        runner_alive = True
        runner_pid = ali_process.pid

    uptime_str = str(datetime.now() - start_time).split('.')[0]

    return jsonify({
        "runner_alive": runner_alive,
        "runner_pid": runner_pid,
        "cpu": cpu,
        "memory": mem,
        "chrome_processes": chrome_count,
        "restart_count": restart_count,
        "uptime": uptime_str,
        "logs": logs
    })

@app.route('/api/agent', methods=['POST'])
def api_agent():
    data = request.json or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"result": "No prompt provided."}), 400
        
    result = run_agent_task(prompt)
    return jsonify({"result": result})

@app.route('/api/restart', methods=['POST'])
def api_restart():
    log_system_activity("User triggered restart of autoresponder.")
    stop_alibaba_automation()
    time.sleep(2)
    success = start_alibaba_automation()
    if success:
        return jsonify({"message": "Successfully restarted Alibaba autoresponder process!"})
    else:
        return jsonify({"message": "Failed to start Alibaba autoresponder process. Check activity.log for details."}), 500

# ------------------ MAIN SERVICE INITIALIZATION ------------------

def start_server_and_automation():
    # Start the Alibaba automation in the background
    start_alibaba_automation()
    
    # Run the Flask app on the correct port (Render defaults to 10000 or $PORT)
    port = int(os.environ.get("PORT", 10000))
    log_system_activity(f"Starting Flask status web server on port {port}...")
    app.run(host="0.0.0.0", port=port)

if __name__ == '__main__':
    start_server_and_automation()
