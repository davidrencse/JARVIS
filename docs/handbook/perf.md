# Performance rules

*Tuning that is still in the code. Old timings and test totals are not.*

- The voice listener unloads idle models only after real speech has stopped. Noise does not reset the idle clock. A failed unload waits `IDLE_TICK` (30 s) before it tries again. A CUDA transcription that is still running is kept until it finishes.
- The voice user unit sets `MemoryMax=3G` and `MemorySwapMax=0` and does not set `MemoryHigh`. A 2800M high-water mark throttled a listener sitting near 2.6 GB with models loaded.
- `listen.py` and `embed.py` set `enable_cpu_mem_arena` false so a long clip or an indexing batch can return its peak.
- Wi-Fi status uses `nmcli … --rescan no`. A rescan costs about 5–8 s. The cached scan is about 0.1 s.
- Disk status is a local `shutil` read of the home drive, on every OS, and does not go through the Linux-only status gate.
- `focus_window` uses an 8 s node timeout. The SSH wrapper adds 5 s. Other remote actions keep their defaults.
- The app list is fetched in the background. An expired list that is not empty is returned at once while one refresh runs. A failed refresh keeps the old list.
- The console caches shared-input and peripheral reads for 12 s, and the project tree for 30 s. A POST clears the short cache before the action and after an async shared-input action. Static files send ETag and Last-Modified and revalidate. Fonts keep a one-hour cache.
- Hidden console tabs skip the ear-level poll and the clock, overview, history, credits, and probe polls, and catch up when the tab is visible.
- The Lan Mouse address watcher does not look the peer up again while `apart_now` is true.

`jarvis route` tries the strict parser first and does not execute in that dry run. Status questions the parser can decide (battery, Wi-Fi, disk, a Spotify player with no named track) stay local. A named artist or song stays unresolved so it can be searched.

Leave these caches alone. They are not one TTL: probe results carry `checked_at` and an explicit refresh; credit caches drop on credential changes and window resets; module and tree caches key off file stamps and AST, not a clock.
