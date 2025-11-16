import sqlite3
import flet as ft
import re
import feedparser
from datetime import datetime, timezone
from time import mktime
from email.utils import parsedate_to_datetime
import json, os
import webbrowser
import traceback
import sys
import asyncio
import aiohttp
import aiosqlite
import threading, requests, time

# Read DEBUG flag from environment or if debugger attached
DEBUG = os.getenv("DEBUG", "0").lower() in ("1", "true", "yes") or (hasattr(sys, "gettrace") and sys.gettrace() is not None)

KEYWORD = "injury"
URL = "https://www.rotoballer.com/feed"
DB_FILE = "feeds.db"
DEFAULT_INDEX = 1

class AppState:
    def __init__(self):
        self.feeds = []
        self.whitelist = []
        self.blacklist = []
        self.bg_loop = {"loop": None}  # holder for background asyncio loop (dict used to allow assignment from inner start_async_loop)
        self.interval_seconds = 0
        self.refresh_event = asyncio.Event()
        self.last_refresh_time = 0
        self.old_refresh = 1
        self.settings = {}

state = AppState()
                        
def main(page: ft.Page):
    ##Functions
   # ---------- Async DB save ----------
    def close_dialog(dialog):
        dialog.open = False
        page.update()

    def alert_error(msg):
        dialog = ft.AlertDialog(
            title=ft.Text("Error"),
            content=ft.Text(msg),
            open=True,
            modal=True,
            actions=[ft.TextButton("OK", on_click=lambda e: close_dialog(dialog))],
        )
        page.open(dialog)

    def safe_execute(db_func, *args, **kwargs):
        try:
            db_func(*args, **kwargs)
        except sqlite3.IntegrityError as e:
            alert_error(f"SQLite IntegrityError: {e}")
            if DEBUG:
                traceback.print_exc()
            return False
        except Exception as e:
            alert_error(f"Unexpected error while saving entry: {e}")
            if DEBUG:
                traceback.print_exc()
            return False
        return True
    
    async def safe_execute_async(*args, **kwargs):
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(*args, **kwargs)
        except aiosqlite.IntegrityError as e:
            alert_error(f"SQLite IntegrityError: {e}")
            if DEBUG:
                traceback.print_exc()
            return False
        except Exception as e:
            alert_error(f"Unexpected error while saving entry: {e}")
            if DEBUG:
                traceback.print_exc()
            return False
        return True
    
    def ping_url(url):
        try:
            resp = requests.head(url, timeout=10)
            if resp.status_code >= 400:
                return False
            return True
        except requests.RequestException:
            return False

    def open_url(url):
        webbrowser.open(url)

    def create_tables():
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        c = conn.cursor()
        # --- Database setup ---
        sql_txt = """
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY,
                url TEXT UNIQUE
            )
        """
        if not safe_execute(c.execute, sql_txt):
            alert_error("Failed to create feeds table")
            sys.exit()
        sql_txt = """
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
        """
        if not safe_execute(c.execute, sql_txt):
            alert_error("Failed to create entries table")
            sys.exit()
        sql_txt = """
            CREATE TABLE IF NOT EXISTS keywords (
                word TEXT PRIMARY KEY,
                type TEXT
            )
        """
        if not safe_execute(c.execute, sql_txt):
            alert_error("Failed to create keywords table")
            sys.exit()
        sql_txt = """
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT
            )
        """
        if not safe_execute(c.execute, sql_txt):
            alert_error("Failed to create keywords table")
            sys.exit()
        conn.commit()
        conn.close()

    def create_feed_row(url):
        def delete_callback():
            remove_feed(url)

        row = ft.Container(
            content=ft.Row([
                ft.Text(f"• {url}", expand=True, overflow="ellipsis"),
                ft.Container(
                    content=ft.Icon(name="REMOVE", size=20),
                    on_click=lambda e: delete_callback(),
                    padding=ft.padding.all(0),
                    border_radius=5,
                )
            ]),
            padding=ft.padding.only(right=12)
        )
        feeds_list_ui.controls.append(row)

    # --- Keyword row UI ---
    def create_keyword_row(word, list_type):
        def delete_callback():
            remove_keyword(word, list_type)

        row = ft.Container(
            content=ft.Row([
                ft.Text(word, expand=True, overflow="ellipsis"),
                ft.Container(
                    content=ft.Icon(name="REMOVE", size=20),
                    on_click=lambda e: delete_callback(),
                    padding=ft.padding.all(0),
                    border_radius=5,
                )
            ]),
            padding=ft.padding.only(right=12)
        )
        if list_type == "whitelist":
            whitelist_ui.controls.append(row)
        else:
            blacklist_ui.controls.append(row)

    def sort_table(col: ft.DataColumnSortEvent):
        index = col.column_index
        def get_sort_value(row):
            cell_content = row.cells[index].content
            
            # Handle Container wrapping
            if isinstance(cell_content, ft.Container):
                cell_content = cell_content.content
            
            # Handle TextButton
            if isinstance(cell_content, ft.TextButton):
                if hasattr(cell_content, 'content') and hasattr(cell_content.content, 'value'):
                    return str(cell_content.content.value or "").lower()
            
            # Handle Text
            if isinstance(cell_content, ft.Text):
                return str(cell_content.value or "").lower()
            
            # Handle string value attribute
            if hasattr(cell_content, 'value'):
                return str(cell_content.value or "").lower()
            
            # Fallback
            return ""
        
        try:
            #if you clicked currently sorted column
            if col.column_index is results_table_ui.sort_column_index:
                #if request to sort ascending and not default column
                if col.ascending and (col.column_index is not DEFAULT_INDEX):
                    #means you clicked the 3 times so revert to default index and sort
                    index = DEFAULT_INDEX
                    ascending = col.ascending
                    results_table_ui.sort_ascending = not ascending
                    results_table_ui.sort_column_index = index
                else:
                    #flip the sort
                    index = col.column_index
                    ascending = not col.ascending
                    results_table_ui.sort_ascending = col.ascending
                    results_table_ui.sort_column_index = index
            #if you clicked new column
            else:
                #sort the column ascending on first click
                index = col.column_index
                ascending = not col.ascending
                results_table_ui.sort_ascending = col.ascending
                results_table_ui.sort_column_index = index
            results_table_ui.rows.sort(
                key=get_sort_value,
                reverse=ascending
            )
            results_table_ui.update()
        except Exception as e:
            print(f"Sort error: {e}")
            if DEBUG:
                traceback.print_exc()

    # create clickable title
    def show_entry_details(entry):
        dialog = ft.AlertDialog(
            title=ft.Text(entry['title'], overflow="ellipsis"),
            content=ft.Column([
                ft.Text(f"Published: {entry.get('published_parsed_tz', entry.get('published',''))}"),
                ft.Text(entry.get('summary', '')),
                ft.TextButton(
                    "Read full post",
                    on_click=lambda e: open_url(entry.get('link', '#')),
                    style=ft.ButtonStyle(
                        padding=ft.padding.all(0),
                        text_style=ft.TextStyle(color=ft.Colors.BLUE)
                    )
                )
            ], tight=True),
            actions=[ft.ElevatedButton("Close", on_click=lambda e: close_dialog(dialog))],
            modal=True,
            open=True,
        )
        page.open(dialog)

    # UI update must be run on main thread
    def update_rss_table(rows):
        keyword = keyword_input.value.strip().lower()
        new_rows = []

        for row in rows:
            entry = {
                "feed_url": row[0],
                "title": row[1],
                "published_parsed_tz": row[2],
                "published": row[3],
                "summary": row[4],
                "link": row[5],
            }
            title_lower = (entry['title'] or "").lower()
            if keyword and keyword not in title_lower:
                continue

            site_name = entry['feed_url'].split("www.")[-1].split(".com")[0]
            display_date = (entry['published_parsed_tz'] or "").replace('T', ' ')[:-6] if entry['published_parsed_tz'] else (entry['published'][5:-6] if entry['published'] else "")

            new_rows.append(
                ft.DataRow(cells=[
                    ft.DataCell(ft.Container(
                        ft.Text(site_name,overflow=ft.TextOverflow.ELLIPSIS),
                        alignment=ft.alignment.top_left,
                        padding=ft.padding.all(0),
                    )),
                    ft.DataCell(ft.Container(
                        ft.Text(display_date,overflow=ft.TextOverflow.ELLIPSIS),
                        alignment=ft.alignment.top_left,
                        padding=ft.padding.all(0),
                    )),
                    ft.DataCell(ft.Container(
                        ft.TextButton(
                            content=ft.Text(
                                entry['title'] or "No Title",
                                overflow=ft.TextOverflow.ELLIPSIS,
                                no_wrap=True,
                            ),
                            style=ft.ButtonStyle(
                                color=ft.Colors.WHITE,
                                alignment=ft.alignment.top_left,
                                padding=ft.padding.symmetric(horizontal=0, vertical=0),
                                text_style=ft.TextStyle(weight=ft.FontWeight.NORMAL),
                                shape=ft.RoundedRectangleBorder(radius=4)
                            ),
                            on_click=lambda e, en=entry: show_entry_details(en),
                            height=20
                        ),
                        alignment=ft.alignment.top_left,
                        padding=ft.padding.all(0),
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    )),
                ])
            )

        def _update_ui():
            results_table_ui.rows = new_rows
            results_table_ui.update()
        
        page.run_thread(_update_ui)
        
    async def save_entry_async(db, feed_url, entry):
        def to_json(attr):
            try:
                return json.dumps(entry.get(attr)) if isinstance(entry, dict) or hasattr(entry, "get") else json.dumps(getattr(entry, attr, None))
            except Exception:
                return None

        # Some feedparser entries use dict-like access; support both
        def attr(e, name, default=""):
            try:
                return e.get(name, default) if isinstance(e, dict) else getattr(e, name, default)
            except Exception:
                return default

        tags_json = None
        try:
            tags_json = json.dumps([t.term for t in entry.tags]) if hasattr(entry, "tags") else None
        except Exception:
            tags_json = None

        guid_is_link = int(attr(entry, "guidislink", False))
        content_json = None
        try:
            content_json = json.dumps(attr(entry, "content", None))
        except Exception:
            content_json = None

        published_parsed_iso = None
        published_parsed_tz_iso = None

        if attr(entry, "published", ""):
            try:
                dt = parsedate_to_datetime(attr(entry, "published"))
                published_parsed_tz_iso = dt.astimezone(timezone.utc).isoformat()
            except Exception:
                if DEBUG:
                    traceback.print_exc()

        if attr(entry, "published_parsed", None):
            try:
                published_parsed_iso = datetime.fromtimestamp(mktime(attr(entry, "published_parsed"))).isoformat()
            except Exception:
                if DEBUG:
                    traceback.print_exc()

        try:
            await db.execute("""
                INSERT OR IGNORE INTO entries (
                    id, feed_url, title, title_detail, link, links, authors, author, author_detail,
                    published, published_parsed, published_parsed_tz, tags, guidislink, summary, summary_detail,
                    content, fetched_at
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
                guid_is_link,
                attr(entry, "summary", ""),
                to_json("summary_detail"),
                content_json,
                datetime.now(timezone.utc).isoformat()
            ))
            await db.commit()
        except Exception as e:
            # If DB write fails, show a UI alert on main thread
            page.run_thread(lambda: alert_error(f"DB save failed for feed {feed_url}: {e}"))
            if DEBUG:
                traceback.print_exc()

    # ---------- Async fetch and save ----------
    async def fetch_and_save_async(url):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as resp:
                    data = await resp.text()

            # Parse feed (feedparser.parse accepts a string)
            feed = feedparser.parse(data)

            # save entries using aiosqlite connection
            async with aiosqlite.connect(DB_FILE) as db:
                for entry in feed.entries:
                    await save_entry_async(db, url, entry)

        except Exception as e:
            # show error on UI thread
            page.run_thread(lambda: alert_error(f"Error fetching {url}: {e}"))
            if DEBUG:
                traceback.print_exc()

    def fetch_rss_from_db():
        # After saving, load entries from sqlite3 (sync read is fine)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        placeholders = ','.join(['?'] * len(state.feeds))
        query = f"""
            SELECT feed_url, title, published_parsed_tz, published, summary, link
            FROM entries
            WHERE feed_url IN ({placeholders})
            ORDER BY published_parsed_tz DESC
        """
        try:
            c.execute(query, state.feeds)
            return c.fetchall()
        finally:
            conn.close()

    # --- Fetch RSS and store entries ---
    async def fetch_rss():
        if not state.feeds:
            page.run_thread(lambda: alert_error("No feeds added."))
            return

        # clear UI (main thread)
        def clear_rows():
            results_table_ui.rows.clear()
        page.run_thread(clear_rows)

        # Run all fetch operations concurrently
        tasks = [fetch_and_save_async(url) for url in state.feeds]
        await asyncio.gather(*tasks)

        rows = fetch_rss_from_db()

        update_rss_table(rows)

    # Helper function to sleep with interruption
    async def interruptible_sleep(event, seconds):
        """Sleep but can be interrupted by event"""
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
            event.clear()  # Reset event
            return True  # Was interrupted
        except asyncio.TimeoutError:
            return False  # Sleep completed normally

    # --- Auto refresh loop (runs in background event loop) ---
    async def auto_refresh_loop():
        while True:
            try:
                await asyncio.sleep(1)
                try:
                    state.interval_seconds = int(refresh_interval_input.value.strip() or 0) * 60
                except Exception:
                    state.interval_seconds = 0
                if state.interval_seconds <= 0:
                    def show_disabled():
                        refresh_countdown_container.controls[0].visible = False 
                        refresh_countdown_container.controls[1].value = "Auto-refresh disabled"
                        refresh_countdown_container.update()
                    page.run_thread(show_disabled)
                    await interruptible_sleep(state.refresh_event, 30)
                    continue
                def show_fetching():
                    refresh_countdown_container.controls[0].visible = True  # Show spinner
                    refresh_countdown_container.controls[1].value = "Fetching..."
                    refresh_countdown_container.update()
                page.run_thread(show_fetching)
                # schedule fetch_rss on the same loop (await it)
                await fetch_rss()
                state.last_fetch_time = time.time()
                def hide_spinner():
                    refresh_countdown_container.controls[0].visible = False  # Hide spinner
                    refresh_countdown_container.update()
                page.run_thread(hide_spinner)
                for remaining in range(state.interval_seconds, 0, -1):
                    minutes = remaining // 60
                    seconds = remaining % 60
                    def update_countdown(m=minutes, s=seconds):
                        refresh_countdown_container.controls[1].value = f"⟳ {m}:{s:02d}"
                        refresh_countdown_container.update()
                    page.run_thread(update_countdown)
                    interrupted = await interruptible_sleep(state.refresh_event, 1)
                    if interrupted:
                        break
            except Exception as ex:
                page.run_thread(lambda: alert_error(f"[AutoRefresh] Error: {ex}"))
                if DEBUG:
                    traceback.print_exc()
                refresh_countdown_container.controls[0].visible = False  # Show spinner
                refresh_countdown_container.controls[1].value = "Error fetching"
                await interruptible_sleep(state.refresh_event, 30)
        
    # --- Feed add/remove ---
    def add_feed():
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        url = feed_input.value.strip()
        if not url:
            return

        # Step 1.a: Validate http/https format
        url_pattern = re.compile(r"^https?://")
        if not url_pattern.match(url):
            # try add https://
            url = "https://" + url
            feed_input.value = url

        # Step 1.b: Validate URL format
        url_pattern = re.compile(r"^(https?://[^\s/$.?#]+\.[^\s]+)$")
        if not url_pattern.match(url):
            alert_error("Please enter a valid URL starting with http:// or https://")
            return

        # Step 2: Try parsing the feed to confirm it's valid
        valid = ping_url(url)
        print(f"{url} valid? {valid}")
        if not valid:
            alert_error("Website does not appear to be valid")
            return
        parsed_feed = feedparser.parse(url)
        if not parsed_feed.entries:
            alert_error("The provided URL does not appear to be a valid RSS/Atom feed.")
            return

        if url and url not in state.feeds:
            state.feeds.append(url)
            create_feed_row(url)
            c.execute("INSERT OR IGNORE INTO feeds (url) VALUES (?)", (url,))
            conn.commit()
            conn.close()
            feed_input.value = ""
            update_rss_table(fetch_rss_from_db())
            page.update()

    def remove_feed(url):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if url in state.feeds:
            state.feeds.remove(url)
            # remove UI row
            def _remove_row():
                feeds_list_ui.controls = [
                    c for c in feeds_list_ui.controls
                    if not (
                        isinstance(c, ft.Container) and
                        isinstance(c.content, ft.Row) and
                        c.content.controls[0].value == f"• {url}"
                    )
                ]
                page.update()
            page.run_thread(_remove_row)

            c.execute("DELETE FROM feeds WHERE url = ?", (url,))
            conn.commit()
            conn.close()
            update_rss_table(fetch_rss_from_db())

    # --- Keyword add/remove ---
    def add_keyword(list_type):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        word = keyword_input.value.strip()
        if not word:
            return
        target_list = state.whitelist if list_type == "whitelist" else state.blacklist
        ui_list = whitelist_ui if list_type == "whitelist" else blacklist_ui
        if word not in target_list:
            target_list.append(word)
            create_keyword_row(word, list_type)
            c.execute("INSERT OR IGNORE INTO keywords (word, type) VALUES (?, ?)", (word, list_type))
            conn.commit()
            conn.close()
            keyword_input.value = ""
            page.update()

    def remove_keyword(word, list_type):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        target_list = state.whitelist if list_type == "whitelist" else state.blacklist
        ui_list = whitelist_ui if list_type == "whitelist" else blacklist_ui
        if word in target_list:
            target_list.remove(word)
            ui_list.controls = [
                c for c in ui_list.controls
                if not (
                    isinstance(c, ft.Container) and
                    isinstance(c.content, ft.Row) and
                    c.content.controls[0].value == word
                )
            ]
            c.execute("DELETE FROM keywords WHERE word = ?", (word,))
            conn.commit()
            conn.close()
            page.update()

    def load_config():
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # --- Load feeds and keywords from DB ---
        c.execute("SELECT url FROM feeds")
        state.feeds = [row[0] for row in c.fetchall()]
        c.execute("SELECT word, type FROM keywords")
        keywords = c.fetchall()
        state.whitelist = [w for w, t in keywords if t == "whitelist"]
        state.blacklist = [w for w, t in keywords if t == "blacklist"]
        c.execute("SELECT name, value FROM settings")
        settings = c.fetchall()

        # Populate settings dict
        for name, value in settings:
            state.settings[name] = value

        # Set defaults if not present
        if "refresh_rate" not in state.settings:
            state.settings["refresh_rate"] = "1"

        refresh_interval_input.value=state.settings["refresh_rate"]

        for url in state.feeds:
            create_feed_row(url)
        for w in state.whitelist:
            create_keyword_row(w, "whitelist")
        for b in state.blacklist:
            create_keyword_row(b, "blacklist")
        conn.commit()
        conn.close()
        # Add update here after all controls are loaded
        page.update()

    def schedule_on_bg(coro_func, *args, **kwargs):
        """Schedule coroutine on background loop. If loop not ready, run in a new temporary loop thread."""
        loop = state.bg_loop["loop"]
        if loop is not None and loop.is_running():
            # create coroutine object here
            coro = coro_func(*args, **kwargs)
            # schedule safely from UI thread to background loop
            return asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            # fallback: run in a temporary thread so button still works before bg loop starts
            def _run_temp():
                try:
                    asyncio.run(coro_func(*args, **kwargs))
                except Exception:
                    if DEBUG:
                        traceback.print_exc()
            threading.Thread(target=_run_temp, daemon=True).start()
            return None

    # ---------- Start a dedicated background asyncio loop ----------
    def start_async_loop():
        loop = asyncio.new_event_loop()
        state.bg_loop["loop"] = loop
        asyncio.set_event_loop(loop)
        state.refresh_event = asyncio.Event()
        # schedule the auto refresh coroutine to run on the loop
        loop.create_task(auto_refresh_loop())
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def trigger_immediate_refresh():
        if(time.time() - state.last_fetch_time) > 30:    
            # Signal event to interrupt sleep
            loop = state.bg_loop["loop"]
            if loop and loop.is_running() and state.refresh_event:
                loop.call_soon_threadsafe(state.refresh_event.set)
            else:
                schedule_on_bg(fetch_rss)
                
    def save_setting(name, value):
        state.settings[name] = value
        sql = "INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)"
        params = (name, value)
        with sqlite3.Connection(DB_FILE) as conn:
            c = conn.cursor()
            safe_execute(c.execute, sql, params)

    def validate_refresh_interval(e):
        value = e.control.value.strip()
        
        if value == state.old_refresh:
            return
        
        try:
            if value == "":
                value = "0"
                e.control.value = "0"
                e.control.update()
            else:
                num = int(value)
                # Clamp to 0-30 range
                clamped = max(0, min(120, num))
                if num != clamped:
                    value = str(clamped)
                    e.control.value = str(clamped)
                    e.control.update()
                    trigger_immediate_refresh()
                
        except ValueError:
            value = "1"
            e.control.value = "1"
            e.control.update()
        save_setting("refresh_rate", value)


    page.title = "Multi-Feed RSS App"
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.padding = 30  # Add padding around entire page

    # UI Elements
    keyword_input = ft.TextField(label="Keyword Filter", value=" ", expand=3)
    feed_input = ft.TextField(label="Add RSS Feed URL", expand=7, value=URL)
    feeds_list_ui = ft.ListView(expand=3, height=80, spacing=3, padding=1, auto_scroll=False)
    whitelist_ui = ft.ListView(expand=2, height=80,  spacing=3, padding=1, auto_scroll=False)
    blacklist_ui = ft.ListView(expand=2, height=80, spacing=3, padding=1, auto_scroll=False)

    # --- Buttons ---
    # refresh button will schedule fetch_rss on background loop
    add_feed_button = ft.ElevatedButton(text="Add Feed", on_click=lambda e: add_feed())
    add_whitelist_button = ft.ElevatedButton("Whitelist", on_click=lambda e: add_keyword("whitelist"))
    add_blacklist_button = ft.ElevatedButton("Blacklist", on_click=lambda e: add_keyword("blacklist"))
    refresh_interval_input = ft.TextField(
        label="refresh(min)", value="1", width=95, text_align=ft.TextAlign.CENTER,
        height=30,text_vertical_align=ft.VerticalAlignment.START,text_size=20,
        input_filter=ft.NumbersOnlyInputFilter(),
        keyboard_type=ft.KeyboardType.NUMBER, 
        on_blur=validate_refresh_interval,
        #hint_text="0-120 0=disabled",
        #suffix_text="0-120",
    )
    refresh_button = ft.ElevatedButton(
        text="Fetch RSS",
        on_click=lambda e: trigger_immediate_refresh()
    )

    refresh_countdown_container = ft.Row([
        ft.ProgressRing(width=20, height=20, stroke_width=2, visible=False),
        ft.Text("", size=13, color=ft.Colors.GREY_400)
    ], spacing=5)
    
    auto_refresh_task = None  # Keep reference if needed

    page.add(ft.Row([feed_input, add_feed_button, keyword_input, add_whitelist_button, add_blacklist_button], spacing=3))
    page.add(
        ft.Row([ft.Text("Feeds:",expand=3), ft.Text("Whitelist:",expand=2), ft.Text("Blacklist:",expand=2)], spacing=5),
        ft.Row([feeds_list_ui, whitelist_ui, blacklist_ui]),
        ft.Row([refresh_button, refresh_interval_input, refresh_countdown_container], spacing=5)
    )

    ##results_list_ui = ft.ListView(expand=True, spacing=5, padding=5, auto_scroll=False)
    ##page.add(ft.Text("Results:"), results_list_ui)
    results_table_ui = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Container(
                    ft.Text("Site", text_align=ft.TextAlign.CENTER),
                    alignment=ft.alignment.center, padding=ft.padding.all(0)
                ),
                on_sort=lambda col: sort_table(col),
                heading_row_alignment=ft.MainAxisAlignment.CENTER
            ),
            ft.DataColumn(ft.Container(
                    ft.Text("Published", text_align=ft.TextAlign.CENTER), 
                    alignment=ft.alignment.center, padding=ft.padding.all(0)
                ), 
                on_sort=lambda col: sort_table(col),
                heading_row_alignment=ft.MainAxisAlignment.CENTER
            ),
            ft.DataColumn(ft.Container(
                    ft.Text("Title"),
                    alignment=ft.alignment.center_left, padding=ft.padding.all(0)
                ), 
                on_sort=lambda col: sort_table(col)
            ),
        ],
        rows=[],
        data_row_max_height=24,
        heading_row_height=30,
    )

    scrollable_table = ft.ListView(
        controls=[results_table_ui],
        #height=400,  # Set fixed height to enable scrolling
        #padding=ft.padding.all(5),
        expand=True
    )

    page.add(scrollable_table)

    create_tables()
    load_config()
    page.update()
    results_table_ui.sort_ascending = False
    results_table_ui.sort_column_index = DEFAULT_INDEX
    page.update()
    
    if not state.feeds:
        time.sleep(10)
    threading.Thread(target=start_async_loop, daemon=True).start()


# ---------- App entry ----------
if __name__ == "__main__":
    ft.app(
        target=main,
        view=ft.FLET_APP
        #view=ft.WEB_BROWSER,
        #port=8550
    )
