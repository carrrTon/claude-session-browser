#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
APP_PATH="$SCRIPT_DIR/app.py"
URL="http://127.0.0.1:8765"
PORT="8765"
LOG_PATH="/tmp/claude-session-browser.log"
PID_PATH="/tmp/claude-session-browser.pid"
CURRENT_TTY="$(tty)"
export CLAUDE_BROWSER_CLI="${CLAUDE_BROWSER_CLI:-claude}"

close_this_terminal_window() {
  /usr/bin/osascript <<OSA >/dev/null 2>&1 &
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "$CURRENT_TTY" then
        close w
        return
      end if
    end repeat
  end repeat
end tell
OSA
}

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "未找到 python3，无法启动 Claude 浏览器。" buttons {"好"} default button 1 with icon stop'
  exit 1
fi

if [ -f "$PID_PATH" ]; then
  OLD_PID="$(cat "$PID_PATH" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && ps -p "$OLD_PID" -o command= 2>/dev/null | grep -F "$APP_PATH" >/dev/null 2>&1; then
    kill "$OLD_PID" 2>/dev/null || true
    sleep 0.5
  fi
fi

EXISTING_PIDS=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$EXISTING_PIDS" ]; then
  for PID in $EXISTING_PIDS; do
    if ps -p "$PID" -o command= 2>/dev/null | grep -F "$APP_PATH" >/dev/null 2>&1; then
      kill "$PID" 2>/dev/null || true
      sleep 0.5
    else
      osascript -e 'display dialog "端口 8765 已被其他程序占用，未启动 Claude 浏览器。" buttons {"好"} default button 1 with icon caution'
      exit 1
    fi
  done
fi

nohup python3 "$APP_PATH" > "$LOG_PATH" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_PATH"

for i in {1..30}; do
  if lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    TOKEN="$(grep -m 1 -o 'token=[A-Za-z0-9_-]*' "$LOG_PATH" 2>/dev/null | head -n 1)"
    if [ -n "$TOKEN" ]; then
      open -a Safari "$URL/?$TOKEN"
    else
      open -a Safari "$URL"
    fi
    close_this_terminal_window
    exit 0
  fi
  sleep 0.2
done

kill "$SERVER_PID" 2>/dev/null || true
osascript -e 'display dialog "Claude 浏览器启动超时，请查看日志：/tmp/claude-session-browser.log" buttons {"好"} default button 1 with icon caution'
exit 1
