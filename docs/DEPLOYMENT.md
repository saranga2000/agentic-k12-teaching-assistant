# Deployment

## Recommendation

**Server: the MacBook Air. Client: the iPad Air M1 in a browser.**

The iPad is the right capture device and the wrong server. It cannot run a Python
process, a background scheduler, or a database, and iPadOS will suspend anything that
tries. Treat it as a very good camera attached to a very good screen.

The Lenovo Celeron machine is not a serious candidate for either role. Leave it out.

### Why not iPad-only

A native or web-only iPad build would require moving grading, persistence, and
scheduling to a cloud service, which adds an account system, hosting cost, and children's
data leaving the house. All three are worse than plugging in a laptop.

## Layout

```
iPad Air M1  (Chrome, ordinary browser tab)  --> http://<macbook>.local:8080
Pixel 9a / iPhone 16 (same URL, backup capture)
MacBook Air  runs uvicorn + sqlite, data stays on disk
```

Setup on the MacBook:

```bash
make install
make seed   # until M3.1's real setup flow exists, seeds two example students
make run    # or: uvicorn k12ta.web.app:app --host 0.0.0.0 --port 8080
```

`make seed` and `make run` must see the same `K12TA_DATA_DIR` (unset is fine — both
default to `./data` — but if you set it, set it for both). A mismatch, or skipping
`make seed` entirely, doesn't error: the student picker renders a normal 200 page with
a "no students yet" message instead of the name buttons, which reads as a blank screen
at a glance.

On the iPad, open `http://<hostname>.local:8080` in Chrome as an ordinary tab. **Do not
add it to the home screen.** `<input type=file capture="environment">` opening the
camera directly is unreliable inside Safari's standalone (home-screen, no-chrome)
launch mode on iOS — a real-device test produced a blank black camera screen with no
way to take a photo, a known category of WebKit bug in that mode. An ordinary browser
tab (Chrome or Safari) doesn't hit it: the camera opens correctly. That drops the
"no browser chrome" requirement from the original M2.2 spec — a live camera that
sometimes doesn't work is a worse trade than a visible address bar.

## The always-on problem

The MacBook has to be awake when a child wants to work, and it is your work machine.
Three options, in order of preference:

1. **Accept it for M0 to M3.** Sessions happen when you are around anyway during the
   first weeks, and you want to watch the early ones.
2. **Keep it plugged in with `caffeinate -s` and the lid open on a shelf.** Costs
   nothing, works immediately, ugly.
3. **Move the server to a dedicated always-on box around M4.** A used Mac mini or a
   Raspberry Pi 5 both run this workload comfortably. This is the right answer once the
   system is part of the routine, and it is a one hour migration because there is no
   cloud state to move.

Add Tailscale once there is anything worth reaching from outside the house. Do not open
a port on the router.

## Model access

Cloud vision and reasoning, no local GPU. The MacBook Air can run a small local model
but not one that reads a child's handwritten long division reliably, and the failure
mode of a weak transcriber is exactly the failure mode this project cannot afford.

Set `K12TA_DAILY_TOKEN_BUDGET_USD` and enforce it. Expected steady-state cost for two
children at one page per day is small, but an accidental retry loop over a photo is not,
and a hard stop is one line of code.

## Backups

`data/` holds the database and the images. One line in a cron job copying it to an
external drive or an encrypted cloud folder. The mastery history is the only thing in
this system that cannot be regenerated.
