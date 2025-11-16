import flet as ft
import websockets
import json
import asyncio
import threading
import json, os, sys
import requests
import webbrowser
import traceback, time, re
from shared import *

# Read DEBUG flag from environment or if debugger attached
DEBUG = os.getenv("DEBUG", "0").lower() in ("1", "true", "yes") or (hasattr(sys, "gettrace") and sys.gettrace() is not None)

DEFAULT_INDEX = 1
SERVICE_URL = "ws://localhost:8000/ws"  # WebSocket URL
URL = "https://www.rotoballer.com/feed"
MAX_RETRIES = 10

class AppState:
    def __init__(self):
        self.feeds = []
        self.whitelist = []
        self.blacklist = []
        self.bg_loop = None
        self.interval_seconds = 0
        self.refresh_event = asyncio.Event()
        self.last_refresh_time = 0
        self.old_refresh = 1
        self.settings = {}
        self.ws = None
        self.ws_connected = False

state = AppState()

def main(page: ft.Page):
    
    # WebSocket connection
    state.ws = None
    state.ws_connected = False
    
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

    def verify_websocket_connected():
        # Wait for WebSocket to connect
        for i in range(MAX_RETRIES):
            if state.ws and state.ws_connected:
                break
            time.sleep(1)
        if not state.ws_connected:
            print(f"Warning: WebSocket not connected after waiting {MAX_RETRIES} seconds")
            return False
        return True

    async def connect_websocket():
        """Connect to WebSocket server"""
        try:
            state.ws = await websockets.connect(SERVICE_URL)
            state.ws_connected = True
            print("WebSocket connected")
            
            # Notify UI
            page.run_thread(lambda: update_connection_status(True))
            
            # Start listening for messages
            await listen_to_websocket()
            
        except Exception as e:
            print(f"WebSocket connection error: {e}")
            state.ws_connected = False
            page.run_thread(lambda: update_connection_status(False))
            
            # Retry connection
            await asyncio.sleep(5)
            await connect_websocket()
    
    async def listen_to_websocket():
        """Listen for messages from server"""        
        try:
            async for message in state.ws:
                data = json.loads(message)
                message_type = data.get("type")
                
                # Handle different message types
                if message_type == "new_entries":
                    # New entry arrived - add to UI
                    page.run_thread(lambda d=data: add_new_entries_to_ui(d["data"]))

                # Handle different message types
                if message_type == "new_entry":
                    # New entry arrived - add to UI
                    break
                    page.run_thread(lambda d=data: add_new_entry_to_ui(d["data"]))
                
                elif message_type == "fetch_started":
                    page.run_thread(lambda: show_fetch_status("Fetching..."))
                
                elif message_type == "fetch_complete":
                    count = data.get("new_entries", 0)
                    page.run_thread(lambda c=count: show_fetch_status(f"Done! {c} new entries"))
                
                elif message_type == "entries":
                    # Received entries list
                    page.run_thread(lambda d=data: create_entries_ui(d["data"]))
                
                elif message_type == "feeds":
                    # Received feeds list
                    page.run_thread(lambda d=data: create_feeds_ui(d["data"]))
                
                elif message_type == "keywords":
                    # Received keywords list
                    page.run_thread(lambda d=data: create_keywords_ui(d["data"]))
            
                elif message_type == "feed_added_success":
                    # Alternative message type from server
                    page.run_thread(lambda d=data: add_feed_to_ui(d["data"], True))
                
                elif message_type == "keyword_added_success":
                    # Alternative message type from server
                    page.run_thread(lambda d=data: add_keyword_to_ui(d["data"], True))
            
                elif message_type == "feed_deleted_success":
                    # Server confirmed deletion - remove from UI
                    url = data["data"].get("url")
                    if url:
                        page.run_thread(lambda u=url: remove_feed_from_ui(u))
                
                elif message_type == "keyword_deleted_success":
                    # Server confirmed deletion - remove from UI
                    word = data["data"].get("word")
                    type = data["data"].get("type")
                    if word and type:
                        page.run_thread(lambda w=word,t=type: remove_keyword_from_ui(w,t))
                
                elif message_type == "error":
                    # Show error message
                    error_msg = data.get("message", "Unknown error")
                    page.run_thread(lambda msg=error_msg: alert_error(msg))
                
        except websockets.exceptions.ConnectionClosed:
            print("WebSocket connection closed")
            state.ws_connected = False
            page.run_thread(lambda: update_connection_status(False))
            
            # Reconnect
            await asyncio.sleep(5)
            await connect_websocket()
    
    async def send_message(message: dict):
        """Send message to server"""
        if state.ws and state.ws_connected:
            try:
                await state.ws.send(json.dumps(message))
            except Exception as e:
                print(f"Error sending message: {e}")
    
    def send_ws_message(message: dict):
        if not verify_websocket_connected():
            print("WebSocket not connected - cannot send message")
            return
        
        if not state.bg_loop:
            print("WebSocket loop not available - cannot send message")
            return
        try:
            # Schedule on the WebSocket loop (use state.bg_loop)
            asyncio.run_coroutine_threadsafe(
                send_message(message),
                state.bg_loop  # use stored loop
            )
        except Exception as e:
            print(f"Error scheduling message: {e}")
            if DEBUG:
                traceback.print_exc()
    
    # UI callback functions
    def update_connection_status(connected: bool):
        if connected:
            connection_status.value = "🟢"
            connection_status.color = ft.Colors.GREEN
        else:
            connection_status.value = "🔴"
            connection_status.color = ft.Colors.RED
        connection_status.update()

    # create clickable title
    def show_entry_details(entry):
        dialog = ft.AlertDialog(
            title=ft.Text(entry.get('title', 'No Title'), overflow="ellipsis"),
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
        
    def add_new_entries_to_ui(entries: list):
        for row in entries:
            entry = {
                "feed_url": row[0],
                "title": row[1],
                "published_parsed_tz": row[2],
                "published": row[3],
                "summary": row[4],
                "link": row[5],
            }
            add_new_entry_to_ui(entry)
        results_table_ui.update()
        state.last_refresh_time = time.time()
    
    def add_new_entry_to_ui(entry: dict, update: bool = False):
        """Add new entry to top of table"""
        title_lower = (entry.get('title') or "").lower()
        site_name = entry['feed_url'].split("www.")[-1].split(".com")[0]
        display_date = (entry.get('published_parsed_tz') or "").replace('T', ' ')[:-6] if entry.get('published_parsed_tz') else ""
        
        new_row = ft.DataRow(cells=[
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
        # Add to top of table
        results_table_ui.rows.insert(0, new_row)
        if(update):
            results_table_ui.update()
    
    def show_fetch_status(message: str):
        fetch_status.value = message
        fetch_status.update()
    
    def create_feeds_ui(feeds: list):
        print(f"create_feeds_ui: {len(feeds)} feeds received")
        feeds_list_ui.controls.clear()
        for feed in feeds:
            add_feed_to_ui(feed)
        page.update()
    
    def create_keywords_ui(keywords: list):
        whitelist_ui.controls.clear()
        blacklist_ui.controls.clear()
        for keyword in keywords:
            add_keyword_to_ui(keyword)
        page.update()
    
    def create_entries_ui(entries: list):
        for entry in entries:
            add_new_entry_to_ui(entry)
        results_table_ui.update()
        state.last_refresh_time = time.time()
        

    
    #------------Request Functions----------
    def request_feeds():
        """Request feeds list from server"""
        send_ws_message({"type": "get_feeds"})
    
    def request_entries():
        """Request entries from server"""
        keyword = None
        send_ws_message({
            "type": "get_entries",
            "keyword": keyword if keyword else None
        }) 

    def request_keywords():
        """Request keywords from server"""
        keyword = None
        send_ws_message({
            "type": "get_keywords",
            "keyword": keyword if keyword else None
        })
    
    def trigger_fetch():
        """Request server to fetch feeds"""
        send_ws_message({"type": "fetch_feeds"})
    
    # Start WebSocket connection in background
    def start_websocket():
        loop = asyncio.new_event_loop()
        state.bg_loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(connect_websocket())

    def remove_feed(url):
        """Remove feed via WebSocket - only remove from UI if server confirms"""
        if not url:
            alert_error("No feed URL to delete")
            return
        if not url not in state.feeds:
            alert_error("feed not found in list")
            return
        send_ws_message({
            "type": "delete_feed",
            "url": url  # We'll need to modify server to accept URL instead of ID
        })
        
    def remove_feed_from_ui(url):
        """Remove feed from UI after server confirms deletion"""
        to_remove = None
        for control in feeds_list_ui.controls:
            if (isinstance(control, ft.Container) and
                isinstance(control.content, ft.Row) and
                len(control.content.controls) > 0 and
                control.content.controls[0].value == f"• {url}"):
                to_remove = control
                break
        
        if to_remove:
            feeds_list_ui.controls.remove(to_remove)  # Direct removal
            show_fetch_status(f"Feed removed: {url}")
            print(f"Removed feed from UI: {url}")
        else:
            print(f"Feed not found in UI: {url}")
        page.update()

    def remove_keyword(word, type):
        """Remove keyword via WebSocket - only remove from UI if server confirms"""
        if not word:
            alert_error("No  keyword to delete")
            return
        
        target_list = state.whitelist if type == "whitelist" else state.blacklist
        if not word not in target_list:
            alert_error("keyword not found in list")
            return
        send_ws_message({
            "type": "delete_keyword",
            "word": word, 
            "word_type" : type, 
        })

    def remove_keyword_from_ui(word, list_type):
        """Remove keyword from UI after server confirms deletion"""
        # Remove from whitelist
        if list_type == "whitelist":
            ui_list = whitelist_ui
        elif list_type == "blacklist":
            ui_list = blacklist_ui
        else:
            print(f"Error unknown list_type {list_type}")
            return
        
        to_remove = None
        for control in ui_list.controls:
            if (isinstance(control, ft.Container) and
                isinstance(control.content, ft.Row) and
                len(control.content.controls) > 0 and
                control.content.controls[0].value == word):
                to_remove = control
                break
        
        if to_remove:

            ui_list.controls.remove(to_remove)  # Direct removal
            show_fetch_status(f"Keyword removed: {word}")
            print(f"Removed keyword from UI: {word}")
        else:
            print(f"Keyword not found in UI: {word}")
        page.update()

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

    def add_feed():
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

        if url and url not in state.feeds:
            send_ws_message({
                "type": "add_feed",
                "url": url
            })
            
            # Clear input after sending
            feed_input.value = ""
            feed_input.update()
            show_fetch_status(f"Adding feed: {url}")

    def add_keyword(keyword_type):
        """Add a keyword (whitelist or blacklist) via WebSocket"""
        word = keyword_input.value.strip().lower()  # keyword input
        
        if not word:
            return
        
        if keyword_type not in ["whitelist", "blacklist"]:
            alert_error("Application Error: Invalid keyword type")
            return
        
        target_list = state.whitelist if keyword_type == "whitelist" else state.blacklist
        if word not in target_list:
            send_ws_message({
                "type": "add_keyword",
                "word": word,
                "keyword_type": keyword_type
            })
            
            # Clear input after sending
            keyword_input.value = ""
            keyword_input.update()
            show_fetch_status(f"Adding {keyword_type} keyword: {word}")

    def add_feed_to_ui(feed_data: dict, update: bool = False):
        """Add feed to UI after server confirms addition - thread-safe"""
        url = feed_data.get("url")
        
        if not url:
            print("No URL in feed data")
            return
        
        # Check if feed already exists in UI
        for control in feeds_list_ui.controls:
            if (isinstance(control, ft.Container) and
                isinstance(control.content, ft.Row) and
                len(control.content.controls) > 0 and
                control.content.controls[0].value == f"• {url}"):
                print(f"Feed already in UI: {url}")
                show_fetch_status(f"Failed to add feed")
                return
        
        # Create and add the new feed row
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
        state.feeds.append(url)
        if update:
            page.update()
        show_fetch_status(f"Feed added: {url}")
        print(f"Added feed to UI: {url}")

    def add_keyword_to_ui(keyword_data: dict, update:bool = False):
        """Add keyword to UI after server confirms addition - thread-safe"""
        word = keyword_data.get("word")
        list_type = keyword_data.get("type")
        
        if not word or not list_type:
            print("Missing word or type in keyword data")
            return
        
        # Select the appropriate UI list
        if list_type == "whitelist":
            ui_list = whitelist_ui
        elif list_type == "blacklist":
            ui_list = blacklist_ui
        else:
            print(f"Invalid keyword type: {list_type}")
            return
        
        # Check if keyword already exists in UI
        for control in ui_list.controls:
            if (isinstance(control, ft.Container) and
                isinstance(control.content, ft.Row) and
                len(control.content.controls) > 0 and
                control.content.controls[0].value == word):
                print(f"Keyword already in UI: {word}")
                show_fetch_status(f"Failed to add keyword")
                return
        
        # Create and add the new keyword row
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
        
        list = state.whitelist if list_type == "whitelist" else state.blacklist
        ui_list.controls.append(row)
        list.append(word)
        if update:
            page.update()
        show_fetch_status(f"{list_type.capitalize()} keyword added: {word}")
        print(f"Added {list_type} keyword to UI: {word}")

    def save_setting(name, value):
        if name and value:
            send_ws_message({
                "type": "save_setting",
                "name": name, 
                "value":value
            })
            show_fetch_status(f"Saving {name}")

    def trigger_immediate_refresh():
        """Trigger immediate fetch if enough time has passed"""
        if (time.time() - state.last_refresh_time) > 30:    
            # For WebSocket version, just trigger server-side fetch
            trigger_fetch()
        else:
            # Too soon, show message
            time_left = 30 - int(time.time() - state.last_refresh_time)
            show_fetch_status(f"Wait {time_left}s before refreshing")


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
            if col.column_index == results_table_ui.sort_column_index:
                #if request to sort ascending and not default column
                if col.ascending and (col.column_index != DEFAULT_INDEX):
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
                if state.old_refresh != value:
                    save_setting("refresh_rate", value)
                trigger_immediate_refresh()
                
        except ValueError:
            value = "1"
            e.control.value = "1"
            e.control.update()
        if state.old_refresh != value:
            save_setting("refresh_rate", value)
        state.old_refresh = value

    page.title = "Multi-Feed RSS App"
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.padding = 30  # Add padding around entire page

    threading.Thread(target=start_websocket, daemon=True).start()

    connection_status = ft.Text("🔴 Connecting...", size=12)
    fetch_status = ft.Text("", size=12, color=ft.Colors.GREY_400)
    
    keyword_input = ft.TextField(
        label="Keyword Filter",
        value=" ",
        expand=3,
    )
    
    refresh_button = ft.ElevatedButton(
        text="Fetch RSS",
        on_click=lambda e: trigger_fetch()
    )
    
    feed_input = ft.TextField(label="Add RSS Feed URL", expand=7, value=URL)
    feeds_list_ui = ft.ListView(expand=3, height=80, spacing=3, padding=1, auto_scroll=False)
    whitelist_ui = ft.ListView(expand=2, height=80,  spacing=3, padding=1, auto_scroll=False)
    blacklist_ui = ft.ListView(expand=2, height=80, spacing=3, padding=1, auto_scroll=False)

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
    )
    

    refresh_countdown_container = ft.Row([
        ft.ProgressRing(width=20, height=20, stroke_width=2, visible=False),
        ft.Text("", size=13, color=ft.Colors.GREY_400)
    ], spacing=5)

    page.add(ft.Row([feed_input, add_feed_button, keyword_input, add_whitelist_button, add_blacklist_button], spacing=3))
    page.add(
        ft.Row([ft.Text("Feeds:",expand=3), ft.Text("Whitelist:",expand=2), ft.Text("Blacklist:",expand=2)], spacing=5),
        ft.Row([feeds_list_ui, whitelist_ui, blacklist_ui]),
        ft.Row([refresh_button, refresh_interval_input, refresh_countdown_container, connection_status, fetch_status], spacing=5)
    )

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
    results_table_ui.sort_column_index = DEFAULT_INDEX
    results_table_ui.sort_ascending = False
    page.update()

     # Wait for WebSocket to connect
    print("Waiting for WebSocket connection...")
    if verify_websocket_connected:
        print("WebSocket connected, loading data...")
        # Initial data load
        request_feeds()
        request_entries()
        request_keywords()
    else:
        print("Exiting App")
        quit

if __name__ == "__main__":
    ft.app(target=main, view=ft.FLET_APP)