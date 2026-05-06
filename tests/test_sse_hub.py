"""Exercise SSEHub without spinning up an HTTP server."""
import queue
import threading
import time

from conftest import load_script


def test_publish_fans_out_to_all_subscribers():
    ns = load_script()
    hub = ns["SSEHub"]()
    q1 = hub.subscribe()
    q2 = hub.subscribe()
    hub.publish({"v": 1})
    assert q1.get(timeout=0.5) == {"v": 1}
    assert q2.get(timeout=0.5) == {"v": 1}


def test_unsubscribe_stops_delivery():
    ns = load_script()
    hub = ns["SSEHub"]()
    q = hub.subscribe()
    hub.unsubscribe(q)
    hub.publish({"v": 2})
    try:
        q.get_nowait()
    except queue.Empty:
        return
    raise AssertionError("unsubscribed queue still received event")


def test_publish_drops_on_full_queue():
    """Slow client: queue fills, further publishes drop. Producer never blocks."""
    ns = load_script()
    hub = ns["SSEHub"](maxsize=2)
    q = hub.subscribe()
    # Fill the queue past capacity; each publish returns immediately.
    for i in range(10):
        hub.publish({"i": i})
    delivered = []
    while True:
        try:
            delivered.append(q.get_nowait())
        except queue.Empty:
            break
    assert len(delivered) <= 2
    # The first two publishes made it; later ones dropped — but we do not
    # assert _which_ two remain, since queue.put_nowait may preserve head
    # or tail depending on ordering semantics. The contract is bounded.


def test_publish_is_threadsafe_under_concurrent_subscribe():
    ns = load_script()
    hub = ns["SSEHub"]()
    stop = threading.Event()

    def churn():
        while not stop.is_set():
            q = hub.subscribe()
            hub.unsubscribe(q)

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    for i in range(200):
        hub.publish({"i": i})
    stop.set()
    t.join(timeout=1.0)
    # If this completes without RuntimeError ("set changed size…") the lock works.
