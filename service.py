import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import feedparser
import json, time
from datetime import datetime, timezone
from time import mktime
from email.utils import parsedate_to_datetime
import asyncio
from typing import List, Optional, Set
import traceback, sys,io
from shared import *

PORT = int(os.getenv("PORT", 8000))
OVERRIDE_DB_FILE = os.getenv('OVERRIDE_DB_FILE', None)
SQLITE_SERVICE_PATH = os.getenv('SQLITE_PRIVATE_PATH', None)
VOLUME_NAME = os.getenv("RAILWAY_VOLUME_NAME", "no volume name")
MOUNT_PATH = SQLITE_SERVICE_PATH if SQLITE_SERVICE_PATH else os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.getenv("MOUNT_PATH","./data"))
DB_FILE = f"{MOUNT_PATH}/feeds" if SQLITE_SERVICE_PATH else f"{MOUNT_PATH}/feeds.db"
DEBUG = os.getenv("DEBUG", "0").lower() in ("1", "true", "yes")  or (hasattr(sys, "gettrace") and sys.gettrace() is not None)

DB_FILE = OVERRIDE_DB_FILE if OVERRIDE_DB_FILE else DB_FILE

app = FastAPI(title="RSS Feed Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AppState:
    def __init__(self):
        self.time_since_refresh = 0.0
        self.settings = {"refresh_rate" : 0}
        self.refresh_event = None


state = AppState()

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        print(f"Client connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
        print(f"Client disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """Send message to all connected clients"""
        disconnected = set()
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"Error sending to client: {e}")
                disconnected.add(connection)
        
        # Remove disconnected clients
        self.active_connections -= disconnected

manager = ConnectionManager()

# Database helper
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY,
            url TEXT UNIQUE
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY,
            feed_url TEXT,
            title TEXT,
            title_detail TEXT,
            link TEXT,
            links TEXT,
            authors TEXT,
            author TEXT,
            author_detail TEXT,
            published TEXT,
            published_parsed TEXT,
            published_parsed_tz TEXT,
            tags TEXT,
            guidislink INTEGER,
            summary TEXT,
            summary_detail TEXT,
            content TEXT,
            fetched_at TEXT
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            name TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            word TEXT PRIMARY KEY,
            type TEXT
        )
    """)
    
    conn.commit()
    conn.close()
def dump_database_to_file():
    """
    Dumps an SQLite database to a text file containing SQL statements.

    Args:
        database_path (str): The path to the source SQLite database file.
        output_file_path (str): The path where the SQL dump file should be saved.
    """
    try:
        # Connect to the SQLite database
        conn = sqlite3.connect("feeds_ws.db")
        
        # Open the output file in write mode ('w')
        # Use io.open for compatibility with different encoding requirements, default is utf-8
        with io.open("seed.txt", 'w', encoding='utf-8') as f:
            # Use iterdump() to get an iterator of SQL statements
            for line in conn.iterdump():
                line = line.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS")
                f.write('%s\n' % line)
        
        print(f"Database 'feeds_ws.db' successfully dumped to 'seed.txt'")

    except sqlite3.Error as e:
        print(f"SQLite error: {e}")
    except IOError as e:
        print(f"File I/O error: {e}")
    finally:
        # Ensure the connection is closed
        if conn:
            conn.close()

# --- Example Usage ---
# Replace 'your_database.db' with the path to your database file
# Replace 'database_dump.sql' with your desired output file name

def seed_initial_data():
    print("SQLite Library Version:", sqlite3.sqlite_version)
    print("pysqlite Wrapper Version:", sqlite3.version)
    print("MOUNT_PATH ", MOUNT_PATH)
    print("DB_FILE ", DB_FILE)
    path = "."
    print(f"--- Subdirectories in {os.path.abspath(path)} ---")
    subdirs = []
    try:
        # Use os.scandir for efficiency
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_dir():
                    subdirs.append(entry.name)
                    print(entry.name)
    except FileNotFoundError:
        print(f"The directory '{path}' was not found.")
    except PermissionError:
        print(f"Permission denied for directory '{path}'.")
    except OSError as e:
        print(f"An OS error occurred: {e}")

    """Seed database with initial data if empty"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    print("Seeding initial data...")
    file_path = "seed.txt"  # Replace with your file's path

    try:
        with open(file_path, 'r') as file:
            content = file.read()
        conn.executescript(content)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Database seed Failed {e}")
        return
    print("Database seeded successfully!")

# Helper function to sleep with interruption
async def interruptible_sleep(event, seconds):
    """Sleep but can be interrupted by event"""
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
        event.clear()  # Reset event
        return True  # Was interrupted
    except asyncio.TimeoutError:
        return False  # Sleep completed normally

def load_settings():
    conn = get_db()
    rows = conn.execute(
        "SELECT name, value FROM settings"
    ).fetchall()
    conn.close()
    if rows:
        for row in rows:
            state.settings[row[0]] = row[1]

@app.get("/health")
async def health():
    return {"status": "healthy"}

# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    
    async def handle_ping():
        """Ping/pong for connection health"""
        await websocket.send_json({"type": "pong"})
    
    async def handle_get_feeds():
        """Get all feeds - reuses REST logic"""
        feeds = await get_feeds()
        await websocket.send_json({"type": "feeds", "data": feeds})
    
    async def handle_get_keywords():
        """Get all keywords - reuses REST logic"""
        keywords = await get_keywords()
        await websocket.send_json({"type": "keywords", "data": keywords})
    
    async def handle_get_entries():
        """Get entries with optional keyword filter - reuses REST logic"""
        keyword = data.get("keyword")
        limit = data.get("limit", 100)
        entries = await get_entries(keyword=keyword, limit=limit)
        await websocket.send_json({"type": "entries", "data": entries})
    
    async def handle_add_feed():
        """Add a new feed - reuses REST logic"""
        url = data.get("url")
        if not url:
            await websocket.send_json({
                "type": "error",
                "message": "URL is required"
            })
            return
        
        try:
            feed = Feed(url=url)
            result = await add_feed(feed)
            # Broadcast already happens in add_feed
            await websocket.send_json({
                "type": "feed_added_success",
                "data": result
            })
        except HTTPException as e:
            await websocket.send_json({
                "type": "error",
                "message": e.detail
            })
    
    async def handle_delete_feed():
        """Delete a feed - reuses REST logic"""
        url = data.get("url")  # Changed from feed_id to url
        if not url:
            await websocket.send_json({
                "type": "error",
                "message": "url is required"
            })
            return
        
        try:
            result = await delete_feed_by_url(url)
            await websocket.send_json({
                "type": "feed_deleted_success",
                "data": {"url": url}
            })
        except Exception as e:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
    
    async def handle_add_keyword():
        """Add a keyword - reuses REST logic"""
        word = data.get("word")
        keyword_type = data.get("keyword_type")
        
        if not word or not keyword_type:
            await websocket.send_json({
                "type": "error",
                "message": "word and keyword_type are required"
            })
            return
        
        try:
            keyword = Keyword(word=word, type=keyword_type)
            result = await add_keyword(keyword)
            # Broadcast already happens in add_keyword
            await websocket.send_json({
                "type": "keyword_added_success",
                "data": result
            })
        except HTTPException as e:
            await websocket.send_json({
                "type": "error",
                "message": e.detail
            })
    
    async def handle_delete_keyword():
        """Delete a keyword - reuses REST logic"""
        word = data.get("word")
        type = data.get("word_type")
        if not word or not type:
            await websocket.send_json({
                "type": "error",
                "message": "word and type is required"
            })
            return
        
        try:
            result = await delete_keyword(word)
            await websocket.send_json({
                "type": "keyword_deleted_success",
                "data": {"word": word, "type": type}
            })
        except Exception as e:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
    
    async def handle_fetch_feeds():
        """Trigger manual feed fetch"""
        asyncio.create_task(fetch_and_broadcast())
        await websocket.send_json({"type": "fetch_started"})
    
    async def handle_get_setting():
        """Get a setting - reuses REST logic"""
        name = data.get("name")
        if not name:
            await websocket.send_json({
                "type": "error",
                "message": "setting name is required"
            })
            return
        
        try:
            result = await get_setting(name)
            await websocket.send_json({
                "type": "setting",
                "data": result
            })
        except HTTPException as e:
            await websocket.send_json({
                "type": "error",
                "message": e.detail
            })
    
    async def handle_save_setting():
        """Save a setting - reuses REST logic"""
        name = data.get("name")
        value = data.get("value")
        
        if not name or value is None:
            await websocket.send_json({
                "type": "error",
                "message": "name and value are required"
            })
            return
        
        try:
            setting = Setting(name=name, value=value)
            await save_setting(setting)
            # Broadcast already happens in save_setting
            await websocket.send_json({
                "type": "setting_saved_success",
                "data": {"name": name, "value": value}
            })
        except Exception as e:
            await websocket.send_json({
                "type": "error",
                "message": str(e)
            })
    
    # ===== MESSAGE ROUTING =====
    handlers = {
        "ping": handle_ping,
        "get_feeds": handle_get_feeds,
        "get_keywords": handle_get_keywords,
        "get_entries": handle_get_entries,
        "add_feed": handle_add_feed,
        "delete_feed": handle_delete_feed,
        "add_keyword": handle_add_keyword,
        "delete_keyword": handle_delete_keyword,
        "fetch_feeds": handle_fetch_feeds,
        "get_setting": handle_get_setting,
        "save_setting": handle_save_setting,
    }
    
    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            
            handler = handlers.get(message_type)
            if handler:
                await handler()
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown message type: {message_type}"
                })
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        traceback.print_exc()
        manager.disconnect(websocket)

# REST endpoints (still available for non-WebSocket clients)
@app.on_event("startup")
async def startup():
    print(VOLUME_NAME)
    # Debug: Print environment and filesystem info
    print("=" * 50)
    print("ENVIRONMENT VARIABLES:")
    for key, value in os.environ.items():
        if 'RAILWAY' in key or 'VOLUME' in key or 'MOUNT' in key:
            print(f"{key} = {value}")
    
    print("\nROOT DIRECTORY CONTENTS:")
    print(os.listdir('/'))
    
    print("\nAPP DIRECTORY CONTENTS:")
    print(os.listdir('/app'))
    
    print("\nCURRENT WORKING DIRECTORY:")
    print(os.getcwd())
    
    print("\nCHECKING MOUNT PATHS:")
    possible_paths = ['/data', '/app/data', '/mnt/data', '/volume/data']
    for path in possible_paths:
        exists = os.path.exists(path)
        print(f"{path}: {'EXISTS' if exists else 'NOT FOUND'}")
        if exists:
            print(f"  Contents: {os.listdir(path)}")
            print(f"  Writable: {os.access(path, os.W_OK)}")
    
    print("=" * 50)
    #dump_database_to_file()
    seed_initial_data()  # Add this line
    init_db()
    load_settings()
    asyncio.create_task(background_fetch_loop())

@app.get("/")
async def root():
    return {
        "status": "RSS Feed Service Running",
        "websocket": "/ws",
        "docs": "/docs"
    }

@app.get("/feeds", response_model=List[Feed])
async def get_feeds():
    feeds = get_feeds_from_db()
    return feeds

@app.post("/feeds", response_model=Feed)
async def add_feed(feed: Feed):
    conn = get_db()
    
    parsed_feed = feedparser.parse(feed.url)
    if not parsed_feed.entries:
        raise HTTPException(status_code=400, detail="The provided URL does not appear to be a valid RSS/Atom feed.")
    try:
        cursor = conn.execute("INSERT INTO feeds (url) VALUES (?)", (feed.url,))
        conn.commit()
        feed_id = cursor.lastrowid
        
        # Notify all WebSocket clients
        await manager.broadcast({
            "type": "feed_added",
            "data": {"id": feed_id, "url": feed.url}
        })
        
        conn.close()
        return {"id": feed_id, "url": feed.url}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Feed already exists")

@app.delete("/feeds/by-url")
async def delete_feed_by_url(url: str):
    """Delete a feed by URL"""
    conn = get_db()
    
    # Delete the feed
    conn.execute("DELETE FROM feeds WHERE url = ?", (url,))
    conn.commit()

    #if rowcount 0 nothing deleted
    deleted = conn.cursor().rowcount
    
    conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Feed not found")
    
    # Notify all WebSocket clients
    await manager.broadcast({
        "type": "feed_deleted",
        "data": {"url": url}
    })
    
    return {"status": "deleted", "url": url}

@app.get("/keywords", response_model=List[Keyword])
async def get_keywords():
    """Get all keywords"""
    return get_keywords_from_db()

@app.post("/keywords", response_model=Keyword)
async def add_keyword(keyword: Keyword):
    """Add a new keyword"""
    conn = get_db()
                
    if keyword.word and keyword.type:
        try:
            conn.execute(
                "INSERT INTO keywords (word, type) VALUES (?, ?)",
                (keyword.word, keyword.type)
            )
            conn.commit()
            conn.close()
            
            # Notify all WebSocket clients
            await manager.broadcast({
                "type": "keyword_added",
                "data": {"word": keyword.word, "type": keyword.type}
            })
            
            return {"word": keyword.word, "type": keyword.type}
        except Exception as e:
            conn.close()
            raise HTTPException(status_code=400, detail=str(e))
    
@app.delete("/keywords/{word}")
async def delete_keyword(word: str):
    """Delete a keyword"""
    conn = get_db()

    conn.execute("DELETE FROM keywords WHERE word = ?", (word,))
    conn.commit()

    #if rowcount 0 nothing deleted
    deleted = conn.cursor().rowcount
    
    conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Keyword not found")
    
    # Notify all WebSocket clients
    await manager.broadcast({
        "type": "keyword_deleted",
        "data": {"word": word}
    })
    
    return {"status": "deleted", "word": word}

@app.get("/entries", response_model=List[Entry])
async def get_entries(keyword: Optional[str] = None, limit: int = 100):
    return get_entries_from_db(keyword, limit)

@app.post("/fetch")
async def trigger_fetch():
    asyncio.create_task(fetch_and_broadcast())
    return {"status": "fetch triggered"}

@app.get("/settings/{name}")
async def get_setting(name: str):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
    conn.close()
    if row:
        return {"name": name, "value": row["value"]}
    raise HTTPException(status_code=404, detail="Setting not found")

@app.put("/settings")
async def save_setting(setting: Setting):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)",
        (setting.name, setting.value)
    )
    conn.commit()
    conn.close()

    if setting.name == "refresh_rate":
        state.settings["refresh_rate"] = setting.value
    
    # Notify all WebSocket clients
    await manager.broadcast({
        "type": "setting_updated",
        "data": {"name": setting.name, "value": setting.value}
    })
    
    return {"status": "saved"}

# Helper functions
def get_feeds_from_db():
    conn = get_db()
    rows = conn.execute("SELECT id, url FROM feeds").fetchall()
    conn.close()
    return [{"url": row["url"]} for row in rows]

def get_keywords_from_db():
    conn = get_db()
    rows = conn.execute("SELECT word, type FROM keywords").fetchall()
    conn.close()
    return [{"word": row["word"], "type": row["type"]} for row in rows]

def get_entries_from_db(keyword: Optional[str] = None, limit: int = 100):
    conn = get_db()
    
    if keyword:
        query = """
            SELECT id, feed_url, title, link, published, published_parsed_tz, summary
            FROM entries
            WHERE title LIKE ?
            ORDER BY published_parsed_tz DESC
            LIMIT ?
        """
        rows = conn.execute(query, (f"%{keyword}%", limit)).fetchall()
    else:
        query = """
            SELECT id, feed_url, title, link, published, published_parsed_tz, summary
            FROM entries
            ORDER BY published_parsed_tz DESC
            LIMIT ?
        """
        rows = conn.execute(query, (limit,)).fetchall()
    
    conn.close()
    
    state.time_since_refresh = time.time()

    return [
        {
            "id": row["id"],
            "feed_url": row["feed_url"],
            "title": row["title"],
            "link": row["link"],
            "published": row["published"],
            "published_parsed_tz": row["published_parsed_tz"],
            "summary": row["summary"]
        }
        for row in rows
    ]

async def fetch_and_broadcast():
    """Fetch feeds and broadcast updates to clients"""
    # Notify clients that fetch is starting
    await manager.broadcast({
        "type": "fetch_started",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.execute("SELECT url FROM feeds")
    urls = [row[0] for row in cursor.fetchall()]
    
    new_entries_count = 0
    new_entries = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                # Check if entry is new
                entry_id = entry.get("id") 
                
                existing = conn.execute(
                    "SELECT id FROM entries WHERE id = ?", 
                    (entry_id,)
                ).fetchone()
                
                if not existing:
                    save_entry(conn, url, entry)
                    new_entries_count += 1
                    new_entries.append(entry)

        except Exception as e:
            print(f"Error fetching {url}: {e}")
    
    conn.commit()
    conn.close()
    
    state.time_since_refresh = time.time()
    # Notify clients that fetch is complete
    await manager.broadcast({
        "type": "fetch_complete",
        "new_entries": new_entries_count,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

    await manager.broadcast({
        "type": "new_entries",
        "data": new_entries
    })

def entry_to_dict(entry, feed_url):
    """Convert entry to dict for broadcasting"""
    def attr(e, name, default=""):
        try:
            return e.get(name, default) if isinstance(e, dict) else getattr(e, name, default)
        except Exception:
            return default
    
    published_parsed_tz_iso = None
    if attr(entry, "published", ""):
        try:
            dt = parsedate_to_datetime(attr(entry, "published"))
            published_parsed_tz_iso = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    
    return {
        "id": attr(entry, "id") or attr(entry, "guid") or attr(entry, "link", ""),
        "feed_url": feed_url,
        "title": attr(entry, "title", ""),
        "link": attr(entry, "link", ""),
        "published": attr(entry, "published", ""),
        "published_parsed_tz": published_parsed_tz_iso,
        "summary": attr(entry, "summary", "")
    }

def save_entry(conn, feed_url, entry):
    """Save entry to database"""
    def attr(e, name, default=""):
        try:
            return e.get(name, default) if isinstance(e, dict) else getattr(e, name, default)
        except Exception:
            return default
    
    def to_json(attr_name):
        try:
            val = attr(entry, attr_name)
            return json.dumps(val) if val else None
        except Exception:
            return None
    
    published_parsed_tz_iso = None
    if attr(entry, "published", ""):
        try:
            dt = parsedate_to_datetime(attr(entry, "published"))
            published_parsed_tz_iso = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    
    published_parsed_iso = None
    if attr(entry, "published_parsed", None):
        try:
            published_parsed_iso = datetime.fromtimestamp(
                mktime(attr(entry, "published_parsed"))
            ).isoformat()
        except Exception:
            pass
    
    tags_json = None
    try:
        tags_json = json.dumps([t.term for t in entry.tags]) if hasattr(entry, "tags") else None
    except Exception:
        pass
    
    try:
        conn.execute("""
            INSERT OR IGNORE INTO entries (
                id, feed_url, title, title_detail, link, links, authors, author, author_detail,
                published, published_parsed, published_parsed_tz, tags, guidislink, summary, 
                summary_detail, content, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            attr(entry, "id", None) or attr(entry, "guid", None) or attr(entry, "link", ""),
            feed_url,
            attr(entry, "title", ""),
            to_json("title_detail"),
            attr(entry, "link", ""),
            to_json("links"),
            to_json("authors"),
            attr(entry, "author", ""),
            to_json("author_detail"),
            attr(entry, "published", ""),
            published_parsed_iso,
            published_parsed_tz_iso,
            tags_json,
            int(attr(entry, "guidislink", False)),
            attr(entry, "summary", ""),
            to_json("summary_detail"),
            to_json("content"),
            datetime.now(timezone.utc).isoformat()
        ))
    except Exception as e:
        print(f"Error saving entry: {e}")

async def background_fetch_loop():
    """Background task to fetch feeds periodically"""
    await asyncio.sleep(10)  # Wait for startup
    while True:
        try:
            refresh = int(state.settings['refresh_rate'])
            if refresh <= 0:
                    await asyncio.sleep(30)
                    continue
            if refresh > 0:
                if (time.time() - state.time_since_refresh) > float((refresh * 60)):
                    print(f"Auto-fetching feeds... (interval: {refresh} mins)")
                    await fetch_and_broadcast()
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(30)
        except Exception as e:
            print(f"Background fetch error: {e}")
            traceback.print_exc()
            if len(manager.active_connections) > 0:  
                await manager.broadcast({
                    "type": "error",
                    "message": "background fetch error"
                })
            await asyncio.sleep(30)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)