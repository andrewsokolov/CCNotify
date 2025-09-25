#!/usr/bin/env python3
"""
Claude Code Notify
"""

import os
import sys
import json
import sqlite3
import subprocess
import logging
import requests
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timezone


class ClaudePromptTracker:
    def __init__(self):
        """Initialize the prompt tracker with database setup"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(script_dir, "ccnotify.db")
        self.setup_logging()
        self.init_database()
    
    def setup_logging(self):
        """Setup logging to file with daily rotation"""
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(script_dir, "ccnotify.log")
        
        # Create a timed rotating file handler
        handler = TimedRotatingFileHandler(
            log_path,
            when='midnight',  # Rotate at midnight
            interval=1,       # Every 1 day
            backupCount=1,   # Keep 1 days of logs
            encoding='utf-8'
        )
        
        # Set the log format
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        
        # Configure the root logger
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
    
    def init_database(self):
        """Create tables and triggers if they don't exist"""
        with sqlite3.connect(self.db_path) as conn:
            # Create main table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    prompt TEXT,
                    cwd TEXT,
                    seq INTEGER,
                    stoped_at DATETIME,
                    lastWaitUserAt DATETIME
                )
            """)
            
            # Create trigger for auto-incrementing seq
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS auto_increment_seq
                AFTER INSERT ON prompt
                FOR EACH ROW
                BEGIN
                    UPDATE prompt 
                    SET seq = (
                        SELECT COALESCE(MAX(seq), 0) + 1 
                        FROM prompt 
                        WHERE session_id = NEW.session_id
                    )
                    WHERE id = NEW.id;
                END
            """)
            
            conn.commit()
    
    def handle_user_prompt_submit(self, data):
        """Handle UserPromptSubmit event - insert new prompt record"""
        session_id = data.get('session_id')
        prompt = data.get('prompt', '')
        cwd = data.get('cwd', '')
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO prompt (session_id, prompt, cwd)
                VALUES (?, ?, ?)
            """, (session_id, prompt, cwd))
            conn.commit()
        
        logging.info(f"Recorded prompt for session {session_id}")
    
    def handle_stop(self, data):
        """Handle Stop event - update completion time and send notification"""
        session_id = data.get('session_id')

        with sqlite3.connect(self.db_path) as conn:
            # Find the latest unfinished record for this session
            cursor = conn.execute("""
                SELECT id, created_at, cwd, prompt
                FROM prompt
                WHERE session_id = ? AND stoped_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            """, (session_id,))

            row = cursor.fetchone()
            if row:
                record_id, created_at, cwd, prompt_text = row

                # Update completion time
                conn.execute("""
                    UPDATE prompt
                    SET stoped_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (record_id,))
                conn.commit()

                # Get seq number and calculate duration
                cursor = conn.execute("SELECT seq FROM prompt WHERE id = ?", (record_id,))
                seq_row = cursor.fetchone()
                seq = seq_row[0] if seq_row else 1

                duration = self.calculate_duration_from_db(record_id)

                # Extract task context from the prompt
                task_context = self.extract_task_context(prompt_text)

                # Create enhanced notification with task context
                project_name = os.path.basename(cwd) if cwd else "Claude Task"
                title = f"{project_name}: {task_context}"
                subtitle = f"job#{seq} done, duration: {duration}"

                self.send_notification(
                    title=title,
                    subtitle=subtitle,
                    cwd=cwd
                )

                logging.info(f"Task completed for session {session_id}, job#{seq}, duration: {duration}, context: {task_context}")
    
    def handle_notification(self, data):
        """Handle Notification event - check for waiting input and send notification"""
        session_id = data.get('session_id')
        message = data.get('message', '')
        
        if 'waiting for your input' in message.lower():
            cwd = data.get('cwd', '')
            
            with sqlite3.connect(self.db_path) as conn:
                # Update lastWaitUserAt for the latest record
                conn.execute("""
                    UPDATE prompt 
                    SET lastWaitUserAt = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (session_id,))
                conn.commit()
            
            self.send_notification(
                title=os.path.basename(cwd) if cwd else 'Claude Task',
                subtitle="Waiting for input",
                cwd=cwd
            )
            
            logging.info(f"Waiting notification sent for session {session_id}")
    
    def calculate_duration_from_db(self, record_id):
        """Calculate duration for a completed record"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT created_at, stoped_at
                FROM prompt
                WHERE id = ?
            """, (record_id,))
            
            row = cursor.fetchone()
            if row and row[1]:
                return self.calculate_duration(row[0], row[1])
        
        return "Unknown"
    
    def calculate_duration(self, start_time, end_time):
        """Calculate human-readable duration between two timestamps"""
        try:
            if isinstance(start_time, str):
                start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            else:
                start_dt = datetime.fromisoformat(start_time)

            if isinstance(end_time, str):
                end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
            else:
                end_dt = datetime.fromisoformat(end_time)

            duration = end_dt - start_dt
            total_seconds = int(duration.total_seconds())

            if total_seconds < 60:
                return f"{total_seconds}s"
            elif total_seconds < 3600:
                minutes = total_seconds // 60
                seconds = total_seconds % 60
                if seconds > 0:
                    return f"{minutes}m{seconds}s"
                else:
                    return f"{minutes}m"
            else:
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                if minutes > 0:
                    return f"{hours}h{minutes}m"
                else:
                    return f"{hours}h"
        except Exception as e:
            logging.error(f"Error calculating duration: {e}")
            return "Unknown"

    def extract_task_context(self, prompt_text):
        """Extract meaningful task context from user prompt using local LLM"""
        if not prompt_text or not prompt_text.strip():
            return "Task completed"

        # Truncate very long prompts to avoid API limits
        truncated_prompt = prompt_text[:500] if len(prompt_text) > 500 else prompt_text

        try:
            payload = {
                "model": "gpt-4.1-mini-latest",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a helpful assistant that creates very short, descriptive task summaries. Extract the main action/task from the user's request in 2-4 words. Examples: 'Fix auth bug', 'Add login form', 'Update README', 'Debug API', 'Create tests'. Focus on the primary action verb and main subject."
                    },
                    {
                        "role": "user",
                        "content": f"Summarize this task in 2-4 words: {truncated_prompt}"
                    }
                ],
                "max_tokens": 20,
                "stream": False,
                "temperature": 0.1
            }

            response = requests.post(
                "http://localhost:8650/sdk/lanyard/v1/chat/completions",
                json=payload,
                timeout=5
            )

            if response.status_code == 200:
                result = response.json()
                logging.info(f"Response: {result}")
                if 'choices' in result and len(result['choices']) > 0:
                    content = result['choices'][0]['message'].get('content')
                    if content and content.strip():
                        summary = content.strip()
                        # Clean up common AI response patterns
                        summary = summary.replace('"', '').replace("'", "")
                        if summary.lower().startswith('task:'):
                            summary = summary[5:].strip()
                        return summary[:30]  # Ensure it fits in notification

        except Exception as e:
            logging.warning(f"Failed to extract task context via LLM: {e}")

        # Fallback: simple keyword extraction
        return self.simple_task_extraction(truncated_prompt)

    def simple_task_extraction(self, prompt_text):
        """Fallback method for extracting task context without LLM"""
        # Common action keywords
        action_keywords = [
            'fix', 'add', 'create', 'update', 'delete', 'remove', 'build', 'install',
            'debug', 'test', 'analyze', 'review', 'refactor', 'optimize', 'deploy',
            'implement', 'modify', 'configure', 'setup', 'migrate', 'merge'
        ]

        words = prompt_text.lower().split()[:10]  # First 10 words

        # Find action word
        action = None
        for word in words:
            clean_word = word.strip('.,!?;:')
            if clean_word in action_keywords:
                action = clean_word
                break

        # Find subject (next few words after action)
        if action:
            try:
                action_index = next(i for i, word in enumerate(words) if word.strip('.,!?;:') == action)
                subject_words = words[action_index + 1:action_index + 3]
                subject = ' '.join(subject_words).strip('.,!?;:')
                return f"{action.title()} {subject}"[:30]
            except (StopIteration, IndexError):
                return action.title()

        # Last resort: first few words
        return ':bad:' + ' '.join(words[:3]).title()[:30]
    
    def send_notification(self, title, subtitle, cwd=None):
        """Send macOS notification using terminal-notifier"""
        from datetime import datetime
        current_time = datetime.now().strftime("%B %d, %Y at %H:%M")
        
        try:
            cmd = [
                'terminal-notifier',
                '-sound', 'default',
                '-title', title,
                '-subtitle', f"{subtitle}\n{current_time}"
            ]
            
            if cwd:
                cmd.extend(['-execute', f'/usr/local/bin/code "{cwd}"'])
            
            subprocess.run(cmd, check=False, capture_output=True)
            logging.info(f"Notification sent: {title} - {subtitle}")
        except FileNotFoundError:
            logging.warning("terminal-notifier not found, notification skipped")
        except Exception as e:
            logging.error(f"Error sending notification: {e}")


def validate_input_data(data, expected_event_name):
    """Validate input data matches design specification"""
    required_fields = {
        'UserPromptSubmit': ['session_id', 'prompt', 'cwd', 'hook_event_name'],
        'Stop': ['session_id', 'hook_event_name'],
        'Notification': ['session_id', 'message', 'hook_event_name']
    }
    
    if expected_event_name not in required_fields:
        raise ValueError(f"Unknown event type: {expected_event_name}")
    
    # Check hook_event_name matches expected
    if data.get('hook_event_name') != expected_event_name:
        raise ValueError(f"Event name mismatch: expected {expected_event_name}, got {data.get('hook_event_name')}")
    
    # Check required fields
    missing_fields = []
    for field in required_fields[expected_event_name]:
        if field not in data or data[field] is None:
            missing_fields.append(field)
    
    if missing_fields:
        raise ValueError(f"Missing required fields for {expected_event_name}: {missing_fields}")
    
    return True


def main():
    """Main entry point - read JSON from stdin and process event"""
    try:
        # Check if hook type is provided as command line argument
        if len(sys.argv) < 2:
            print("ok")
            return
        
        expected_event_name = sys.argv[1]
        valid_events = ['UserPromptSubmit', 'Stop', 'Notification']
        
        if expected_event_name not in valid_events:
            logging.error(f"Invalid hook type: {expected_event_name}")
            logging.error(f"Valid hook types: {', '.join(valid_events)}")
            sys.exit(1)
        
        # Read JSON data from stdin
        input_data = sys.stdin.read().strip()
        if not input_data:
            logging.warning("No input data received")
            return
        
        data = json.loads(input_data)
        
        # Validate input data
        validate_input_data(data, expected_event_name)
        
        tracker = ClaudePromptTracker()
        
        if expected_event_name == 'UserPromptSubmit':
            tracker.handle_user_prompt_submit(data)
        elif expected_event_name == 'Stop':
            tracker.handle_stop(data)
        elif expected_event_name == 'Notification':
            tracker.handle_notification(data)
    
    except json.JSONDecodeError as e:
        logging.error(f"JSON decode error: {e}")
        sys.exit(1)
    except ValueError as e:
        logging.error(f"Validation error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()