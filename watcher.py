#!/usr/bin/env python3
"""
File watcher that automatically restarts SearXNG when settings change.
"""

import subprocess
import time
import sys
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


def log(msg):
    """Print with immediate flush for background processes."""
    print(msg, flush=True)

WATCH_PATH = "/workspaces/test/searxng"
DEBOUNCE_SECONDS = 2  # Wait before restarting to batch multiple changes


class SearXNGRestartHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_restart = 0
    
    def on_modified(self, event):
        if event.is_directory:
            return
        
        # Only react to settings.yml changes
        if not event.src_path.endswith('settings.yml'):
            return
        
        # Debounce - don't restart if we just restarted
        now = time.time()
        if now - self.last_restart < DEBOUNCE_SECONDS:
            return
        
        self.last_restart = now
        log(f"[Watcher] Detected change in {event.src_path}")
        log("[Watcher] Restarting SearXNG...")
        
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", "/workspaces/test/docker-compose.yml", "restart", "searxng"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                log("[Watcher] SearXNG restarted successfully!")
            else:
                log(f"[Watcher] Error: {result.stderr}")
        except Exception as e:
            log(f"[Watcher] Failed to restart: {e}")


if __name__ == "__main__":
    log(f"[Watcher] Watching {WATCH_PATH} for changes...")
    log("[Watcher] Press Ctrl+C to stop")
    
    handler = SearXNGRestartHandler()
    observer = Observer()
    observer.schedule(handler, WATCH_PATH, recursive=False)
    observer.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    
    observer.join()
    log("[Watcher] Stopped")

