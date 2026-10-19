# Privacy

**We don't use your data to identify you, build a profile of you, or track you.**

## Your photos stay on your computer

The photos you take are analyzed on your own computer (or, on the hosted site,
inside your own browser). They are never uploaded. Only the numbers described
in [`DATA.md`](DATA.md) can be sent, and only if you choose to share.

## Sharing is your choice

The first time you start the app it asks whether to share anonymous
measurements. Nothing is pre-selected. You can change your mind at any time in
Settings, and you can delete anything you shared (see below).

## What is and isn't collected

The exact list of fields is in [`DATA.md`](DATA.md). In short, it's phone camera
model and color data, monitor model identifiers, and the numbers measured from
the chart. It's never photos, location, exact dates, serial numbers, names, file
names or free text you typed.

The shared database has no accounts, cookies or device IDs. Submissions can't
be linked to each other or to you.

## Reference profiles

If you loaded an ICC profile or report values for your reference monitor, its
primaries and white point (numbers only) are stored under that monitor model and
picture mode, and deleted with the rest of that contribution. They are shown to
others only when at least 5 independent people agree.

## Monitor settings

If you answer "did you calibrate it?" and press Share, the Red, Green and Blue
numbers you typed, your answer and the server's own measurement of that photo are
stored under your monitor's model and picture mode. They're anonymous like
everything else, and deleted with the rest of that contribution. Suggestions are
only ever shown when at least 5 independent people agree.

## IP addresses

Hosting providers (Vercel, Supabase, and Cloudflare Turnstile on the hosted
site) process your IP address briefly to deliver the request and protect
against abuse. That is how the internet works, and we can't turn it off.

On our side:
- The raw IP is never written to the database or to application logs.
- For rate limiting we keep a salted hash that changes every day. These rows
  are deleted after 48 hours.
- To count independent contributors we keep a hash of your network prefix with a
  monthly salt, and delete it after 60 days. Old salts are destroyed, so these
  hashes can't be reversed or linked across months.

## Deleting what you shared

Every submission gets a random deletion code, stored only on your computer
(Settings → Your contributions). The server stores only a hash of it, so we
can't tell which submissions are yours. Press Delete next to a contribution and
it is removed from the database and from the shared corrections.

## Using the app without sharing

If you decline, the app works fully offline. It makes no network requests to
the shared database.
