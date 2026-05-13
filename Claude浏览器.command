#!/bin/zsh
set -e

APP_PATH="/Users/f/Documents/CODE/^Project/claude-session-browser/app.py"
URL="http://127.0.0.1:8765"
PORT="8765"
LOG_PATH="/tmp/claude-session-browser.log"
CURRENT_TTY="$(tty)"

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

EXISTING_PIDS=$(lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$EXISTING_PIDS" ]; then
  kill $EXISTING_PIDS 2>/dev/null || true
  sleep 0.5
fi

nohup python3 "$APP_PATH" > "$LOG_PATH" 2>&1 &
SERVER_PID=$!

for i in {1..30}; do
  if lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    open -a Safari "$URL"
    close_this_terminal_window
    exit 0
  fi
  sleep 0.2
done

kill "$SERVER_PID" 2>/dev/null || true
osascript -e 'display dialog "Claude 浏览器启动超时，请查看日志：/tmp/claude-session-browser.log" buttons {"好"} default button 1 with icon caution'
exit 1
