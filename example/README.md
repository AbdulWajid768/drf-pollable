# drf-pollable example project

A minimal Django project wired up with `drf-pollable`, useful for poking at the
endpoints by hand.

```bash
cd example
python manage.py migrate
python manage.py seed
python manage.py runserver
```

It uses the checked-out package directly (no install needed) and needs only
Django and DRF.

## Try it

```bash
# the headline case: poll one field
curl -i http://localhost:8000/api/poll/order-status/1/

# an unchanged poll is nearly free -- reuse the ETag you just got
curl -i -H 'If-None-Match: "<paste the ETag>"' http://localhost:8000/api/poll/order-status/1/

# ask to poll more slowly (allowed)
curl -i "http://localhost:8000/api/poll/order-status/1/?interval=45"

# ask to poll faster than the floor (clamped)
curl -i "http://localhost:8000/api/poll/order-status/1/?interval=1"

# exponential backoff: run this repeatedly and watch Retry-After grow
curl -i http://localhost:8000/api/poll/order-quiet/1/

# a scalar aggregate -- no object id in the URL
curl -i http://localhost:8000/api/poll/unread-count/

# delta feed: take the "cursor" from the response and send it back
curl -i "http://localhost:8000/api/poll/notifications/?since=<cursor>"

# what is registered, and with which limits?
python manage.py list_polls
```

## Watch `done` flip

```bash
python manage.py shell -c "
from shop.models import Order
Order.objects.filter(pk=1).update(status='delivered')"

curl -s http://localhost:8000/api/poll/order-status/1/
# {"value": "delivered", "done": true, ...}  -> the client stops polling
```

The endpoints themselves are declared in [`shop/polls.py`](shop/polls.py).
