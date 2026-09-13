# Reading the TUI: the Explorer, Monitor Center and FS Overview

*A field guide to what every panel on DiskTide's three main screens is drawing,
and how to read it. Written against v0.3.0.*

DiskTide has three main screens, one number key each: `1` Explorer, `2` Monitor
Center, `3` FS Overview (`4` is Cleanup, off by default). Most of what they show
is denser than a table: rings, rectangles, one-character marks and colour, each
of which means something specific. This post goes through them panel by panel.

A few keys work everywhere: `?` shows the key map of the screen you are on, `,`
opens Settings, `Ctrl+P` searches every action by name, and `q` quits.

Every figure below is a real frame of the TUI, not a mock-up: DiskTide running
in tmux at 150×46, captured with its colours and rasterized cell for cell. The
numbered pins are the only thing drawn on top. `tool/gen_blog_shots.py`
regenerates all of them. The data behind them is made up, though: a staged
home directory for the Explorer and Monitor Center, and an invented server for
FS Overview.

## The home directory in the pictures

The Explorer and Monitor figures all show the same synthetic home directory,
`/tmp/home`. It was built so that the four size metrics disagree and its
history has something to say:

| Path | What it is | Why it is there |
|------|------------|-----------------|
| `vm/dev-box.qcow2` | a 512 MiB disk image, 96 MiB of it allocated | a sparse file: Logical and Allocated part ways |
| `backup/photos-2024/` | hardlinks to the 160 JPEGs in `photos/2024/` | Allocated counts them twice, Unique once |
| `private/`, `projects/legacy/secrets/` | mode `000` | unreadable directories make the scan partial |
| `latest-dataset` | a symlink to `datasets/climate/` | symlinks are listed, never followed |
| `datasets/`, `models/` | CSV, Parquet, HDF5, safetensors, checkpoints | the *data* category |
| `photos/`, `papers/`, `archives/`, `downloads/` | JPEG, MP4, PDF, tarballs, an ISO | *media*, *docs* and *archive* |
| `.cache/`, `projects/analysis/.venv/`, `projects/webapp/node_modules/` | package caches and dependencies | regenerable containers, all *ephemeral* |

That comes to 1.4 GiB logical (989.4 MiB allocated, 942.2 MiB unique) in 2,356
files and 171 directories.

A monitor on `/tmp/home` holds 34 snapshots over 31 days: one a day, plus two
days sampled in bursts, the newest on 12 September 2026.
Into that month went a build log growing 250 KiB a day, a 5 MiB checkpoint every
third day, and a dozen CSVs landing in `datasets/climate/raw/` four times.
`node_modules/` appeared on day 8, forty photos on day 12, and a 25 MiB backup
tarball on day 15 that was deleted on day 24. The pip cache was emptied on day
20 and rebuilt on day 27. On day 28 the disk image allocated another 32 MiB
without changing its logical size.

The last day has one change of every kind. The thesis PDF grew 1.7 MiB, a cached
model was deleted, a Parquet file and a whole `downloads/` directory appeared,
and `papers/drafts/` was removed. A few files changed again after that last
snapshot, so the live scan and the newest snapshot disagree slightly, which
matters for Diff.

## Explorer: the sunburst (`F1`)

`disktide PATH` scans a directory and opens the Explorer on it. A bare
`disktide` starts at a welcome screen that asks for the path. The tree on the
left lists the directory, sorted by the active metric. The panel on the right
is one of three views on tabs: Sunburst, Treemap and Details.

The sunburst is the default, and it reads in one sentence: **one ring per level
of depth, length along the ring is share of the parent, colour is the kind of
file.**

![The Explorer on /tmp/home right after the scan. On the left, the tree led by vm/ at 512.0 MiB (36.6%), each row with a six-character history sparkline, a share bar and a percentage; projects/ carries a "1 hidden" badge and private/ a red warning sign. On the right, the sunburst in the tiles shape: rectangular rings around a centre reading "home 1.4 GiB", the grey vm/ segment wrapping a third of the second ring, and a legend reading other 37%, data 28%, media 15%, ephemeral 10%, docs 7%, archive 3%. Twelve numbered pins mark the parts described below.](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/explorer-sunburst.png)

*The Explorer right after the scan: cursor on the root, metric Logical, ring
shape `tiles`.*

1. **Breadcrumb.** The path of the current root. A path too long for the line
   folds in the middle, so the directory you are in stays visible. `◐` means
   the scan could not read everything under it.
2. **Indicator.** The sort order, the metric and the mode. Sorting is by size
   under the active metric (hence `Sort: Logical`), and `s` cycles on to name
   and modification time. `t` changes the metric (see *Four ways to measure*).
   The mode is `Current` for this scan, or the snapshot pair while Diff is on.
3. **History sparkline.** This path's size in the six most recent snapshots of
   the root, drawn with `.:-=+*#` from the path's own lowest value to its own
   highest. `======` is a path that did not change. Blanks at the front stand
   for snapshots from before the path existed. The column only appears once the
   root has snapshots. `tmp/` is the log that grows every day.
   To the right of each row are a bar and a percentage: the row's share of the
   root under the current metric. Every bar starts in the same column and is
   drawn to the same scale. A row whose name leaves no room keeps its
   percentage and drops the bar.
4. **`◐ N hidden`.** N entries at or below this row could not be read, so its
   sizes are lower bounds. The badge is bright when some of those entries sit
   directly in the directory, and dim when all of them are further down.
5. **`⚠`.** A directory the scan could not open at all. It shows 0 Bytes because
   nothing in it could be counted, not because it is empty.
6. **View tabs.** `F1` Sunburst, `F2` Treemap, `F3` Details. Browser terminals
   claim `F1` and `F3` for themselves; there, `Tab` to the tabs and use
   `←`/`→`, or use `Ctrl+P`.
7. **Centre.** The current root and its total under the current metric.
   Clicking it goes up one level, like `u`.
8. **Innermost ring.** The root itself, so it is always a full ring. Hovering it
   says `home (this root)` and that a click goes up.
9. **Second ring.** The root's direct children. A segment's length along the ring
   is its share: `vm/` holds 36.6% of the bytes, so its grey segment covers a
   little over a third of the ring. Thin dark seams separate neighbours where
   there is room to draw them.
10. **Outer rings.** Each ring is one level deeper, down to four levels below the
    root (two while a scan is still running). Files get segments too.
11. **Legend.** Each category's share of the bytes under the root. It counts
    bytes whatever `t` is set to, and `by bytes` says so. Categories under 1%
    fold into a single `<1%` entry.
12. **Footer.** The everyday keys. `?` lists all of them.

### Colour: the kind of file

A file's category comes from its extension:

| Category | Some of its extensions |
|----------|------------------------|
| code | `py` `js` `ts` `c` `cpp` `go` `rs` `sh` `html` `css` `ipynb` |
| docs | `pdf` `md` `txt` `tex` `json` `yaml` `toml` `xml` `lock` |
| data | `csv` `parquet` `h5` `npy` `pkl` `sqlite` `pt` `safetensors` `onnx` |
| media | `png` `jpg` `svg` `gif` `mp4` `mov` `wav` `flac` |
| archive | `zip` `tar` `gz` `xz` `zst` `7z` `iso` `deb` `rpm` |
| ephemeral | `pyc` `so` `o` `whl` `class` `log` `out` |
| other | anything else, like the `.qcow2` disk image |

Two rules sit on top of the extensions.

- Everything inside a regenerable container (`.venv`, `venv`, `node_modules`,
  `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`,
  `.eggs`, `.cache`) is *ephemeral*, whatever its extension. The `.py` files in
  a virtualenv are installed payload, not your code.
- A directory takes the colour of whichever category dominates its bytes. It is
  drawn on a darker, less saturated ladder than files, so a folder never reads
  as a file. A directory with no clear majority stays grey.

The exact colours come from the theme, which you pick in Settings (`,`).

### Pointing at things

The chart follows the tree. Move the cursor to `datasets/` and its segment in
the second ring brightens. Walking the tree with `↓` is the quickest way to
learn which segment is which. Hover any segment and a tooltip gives its name,
its size under the current metric, and its share of the root.

![The Explorer with the tree cursor on datasets/. Its segment in the second ring of the sunburst is drawn brighter, and a tooltip over it reads "datasets, 280.3 MiB · 20%".](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/sunburst-hover.png)

*Cursor on `datasets/`, mouse over its segment: 280.3 MiB, 20% of the root.*

`i` goes into the highlighted directory, which becomes the root of the tree,
the chart and the breadcrumb. It reuses the tree the scan already built. It
rescans only when that tree cannot answer: for a symlink's target, a directory
the scan policy skipped, or a subtree cut short by `max_depth`. Clicking a
segment does the same. `u`, or a click on the centre, goes back up.

`Enter` just opens or closes the row under the cursor, like `→` and `←`. `r`
rescans the directory the scan started from, after asking.

<img alt="The sunburst with the mouse over its innermost ring; the tooltip reads &quot;home (this root), 1.4 GiB · click to go up&quot;."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/sunburst-root-ring.png" width="49%">
<img alt="The Explorer after pressing i on datasets/: the breadcrumb reads / > tmp > home > datasets, the tree holds climate/ (85.0%) and imagery/ (15.0%), the sunburst is entirely data-coloured, and its legend reads data 100% and 1 more &lt;1%."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/sunburst-drill.png" width="49%">

*Left: the innermost ring is the root itself. Right: `i` on `datasets/`. It
has two children, both data, and the legend folds everything else into
`1 more <1%`.*

### Three shapes (`g`)

<img alt="The same sunburst in the disc shape: round rings cut by rays from the centre, with anti-aliased edges."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/sunburst-disc.png" width="49%">
<img alt="The same sunburst in the fill shape: rectangular rings that fill the panel, still cut by rays, which leaves diagonal stair-stepped seams."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/sunburst-fill.png" width="49%">

*`disc` on the left, `fill` on the right.*

`g` cycles the ring shape and saves it to `config.toml` (`ring_shape` under
`[ui]`).

- **tiles** (the default) draws rectangular rings cut by straight lines. Every
  edge lands on a cell edge, so every cell is one flat colour, in any font, in
  a browser terminal, at any cell aspect. The price is the radial reading: a
  segment's children are not along a ray from the centre but in the band
  directly outside it.
- **disc** is the classic sunburst: round rings cut by rays, anti-aliased with
  half-block characters.
- **fill** keeps the rays but squares the rings off to fill the panel.

`disc` and `fill` need the terminal's cell aspect to be drawn true. DiskTide
measures it where the terminal reports pixel sizes and otherwise assumes 2.0.
Settings → Cell aspect overrides it.

## Explorer: the treemap (`F2`)

The treemap spends area instead of angle. Each rectangle's area is its size
under the current metric, laid out three levels deep (two while scanning). It
answers "what are the biggest things here" faster than the rings do, and it
has room to label more of them. The colours are the sunburst's categories, a
shade darker at each level down.

![The same home as a treemap. The 512.0 MiB dev-box.qcow2 fills the upper left; datasets/climate with raw at 185.8 MiB sits below it; a column on the right holds photos, models, .cache, papers, backup, projects and archives, each with a title row. Eight numbered pins mark the parts described below.](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/explorer-treemap.png)

*The same home as a treemap.*

1. **Title row.** The current root, with `◐` when the scan was partial.
2. **A top-level directory** gets a name row and a frame, and its contents are
   drawn inside the frame.
3. **A deeper directory** (`llama-mini` under `models`) gets a title row when it
   is wide enough for the name and tall enough to spare the row.
4. **`… 145 more`.** Entries too small to draw fold into one cell that says how
   many there are and how much they hold. Here that is most of the photo
   mirror in `backup/`. A branch holding the cursor is never folded away,
   however small it is.
5. **A file with room for a label** shows its name and size.
6. **A directory at the depth limit** (`raw`, three levels down) is drawn as one
   cell with its total.
7. **`◐` after a name** means something under that directory could not be read.
8. **Runs of similar files** alternate a slight brightness step, so the ten
   checkpoints read as ten files rather than one block.

![The treemap with the mouse over dev-box.qcow2; the tooltip reads "dev-box.qcow2, 512.0 MiB · 37%".](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/treemap-hover.png)

*Hovering and clicking work as in the sunburst. The tooltip's share is the one
the tree prints: 37% here, 36.6% in the tree.*

## Four ways to measure (`t`)

`t` cycles the metric. The tree, its sort order, both charts and Details all
switch together. Each metric answers a different question, and on the same
tree they can rank things completely differently, which is the point of having
four.

| Metric | What it counts | In this home |
|--------|----------------|--------------|
| Logical | `st_size` of files and symlinks: the bytes a program reads. A directory's own size is not counted. | 1.4 GiB, and `vm/` leads with 512.0 MiB (36.6%). |
| Allocated | `st_blocks × 512` of every file, symlink and directory: the blocks on disk, with every hardlink path counted. | 989.4 MiB. The sparse image drops to 96.0 MiB and `datasets/` leads. `photos/` (159.9 MiB) and `backup/` (47.4 MiB) both count the mirrored JPEGs. |
| Unique | Allocated, with each hardlinked inode counted once, which is what `du -s` reports for a tree with hardlinks. | 942.2 MiB. `photos/` drops to 112.7 MiB and `backup/` keeps its 47.4 MiB. |
| Files | Regular files and symlinks. | 2,356, and `projects/` leads with 1,650 (70.0%). `node_modules/` alone holds 1,080. |

Unique charges a shared inode to whichever of its paths sorts first. That is
deterministic, but not always the path you would pick: `backup/` sorts before
`photos/`, so the mirror keeps the bytes and the originals are what shrink.
Pressing `t` shows a toast naming the new metric for two seconds. The sunburst's
legend keeps counting bytes throughout.

<img alt="The Explorer under Allocated with the treemap: the tree reads home 989.4 MiB, led by datasets/ at 28.3%; vm/ is down to 96.0 MiB and its dev-box.qcow2 rectangle is a small grey block at the top right."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/metric-allocated.png" width="49%">
<img alt="The Explorer under Unique with the treemap: home 942.2 MiB; photos/ has dropped to 112.7 MiB while backup/ still holds 47.4 MiB."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/metric-unique.png" width="49%">

*Left: Allocated, where the sparse image collapses to 96 MiB. Right: Unique,
where `photos/` shrinks and `backup/` does not.*

![The Explorer under Files with the treemap: home 2,356 files, projects/ at 70.0% with 1,650 files; the treemap is dominated by node_modules (1,080 files) and .venv (397 files).](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/metric-files.png)

*Files: a pile of small files that is invisible by bytes and dominant by count.*

## Details (`F3`)

Details is the text behind the highlighted row. On the root of this home:

![The Details panel for /tmp/home: Logical ≥ 1.4 GiB (partial), Allocated ≥ 989.4 MiB (partial), Unique on disk ≥ 942.2 MiB (partial), Own Logical 16 Bytes, Own Allocated 4.0 KiB, Files 2,356, Subdirs 171, a Policy line, an Access line reading "Partial — 1 unreadable in this directory · 2 hidden at or below", the modification time, and the Top Items list.](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/explorer-details.png)

*Details for the root.*

- **Logical, Allocated, Unique on disk** carry `≥` and `(partial)` because two
  directories below could not be read. The true totals are at least this much.
- **Own …** counts only what sits directly in this directory, not in its
  subdirectories: the 16-byte symlink under Logical, plus the directory's own
  4 KiB block under Allocated and Unique.
- **Files and Subdirs** count everything below.
- **Policy** is the scope the scan ran with: whether it crossed filesystems,
  what it excluded, how deep it went, and that symlinks are never followed.
- **Access** separates the two kinds of unreadable. `1 unreadable in this
  directory` is `private/`; `2 hidden at or below` adds
  `projects/legacy/secrets/`.
- **Top Items** lists the largest children and their shares under the active
  metric.

On a hardlinked file, Details adds **Hard links** and **Unique owner**, the path
that Unique charges the shared bytes to. The file's row in the tree says the
same thing in brackets: `[hardlink → …]` on a duplicate, and
`[hardlink owner; 2 links]` on the path that owns the bytes.

## Diff: what changed between two snapshots (`d`)

Once a root has at least two compatible snapshots, from a monitor or from
`disktide scan --snapshot`, `d` turns the Explorer into a diff. It compares
**two saved snapshots**, the newest two by default, and never the scan on
screen.

Several things tell you so. The indicator names the pair
(`Diff snapshots #33 → #34`), a toast says it when you switch Diff on, and the
sizes in the tree become the newer snapshot's. That is why `datasets/` reads
278.0 MiB here but 280.3 MiB a moment earlier: four Parquet files changed after
snapshot #34 was taken.

`[` and `]` step to newer and older pairs, and `d` again returns to this scan.

![The Explorer in Diff mode. The indicator reads "Diff snapshots #33 → #34". Tree rows carry deltas: datasets/ "≈ ▲ +6.0 MiB (+2.2%)", .cache/ "≈ ▼ -20.0 MiB (-19.3%)", downloads/ "≈ ＋ +16.0 MiB (+100.0%)", and a final "… remainder/" row. The sunburst is recoloured by direction: most of it grey, datasets red, .cache green, thin amber slivers for downloads and a blue one for the removed drafts. The centre reads "home ≈ ▲ +1.0 MiB (+0.1%)". Seven numbered pins mark the parts described below.](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/diff-sunburst.png)

*Diff over the newest pair of snapshots.*

1. **The pair**, written baseline → target.
2. **Growth.** `▲ +6.0 MiB (+2.2%)` is the change, and the change as a
   percentage of the baseline value.
3. **Shrink.** `▼ -20.0 MiB (-19.3%)`: a cached model was deleted from `.cache/`.
4. **New.** `＋ +16.0 MiB (+100.0%)`: `downloads/` did not exist in #33. A removed
   path gets `×`.
5. **`… remainder/`.** The diff keeps the largest paths and the paths that
   changed most. Everything else is summed into remainder rows, so the totals
   stay exact.
6. **Centre.** The root's net change.
7. **Legend.** In Diff, colour is direction instead of file type. In the default
   theme growth is red, shrink green, new amber and removed blue; unchanged is
   grey.

Every delta here starts with `≈`, which is a hedge. This home has unreadable
directories, so every snapshot of it is partial and every number is a lower
bound. The direction is still what was measured. Only a path that was itself
unreadable in one of the two snapshots is drawn as `≈ partial`, with no
direction.

![The same pair as a treemap. Frames are coloured by net direction: the home frame red for growth, .cache and papers green for shrink, an amber block for downloads; unchanged rectangles such as dev-box.qcow2 are grey and labelled "≈ · 0 Bytes (+0.0%)".](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/diff-treemap.png)

*The same pair as a treemap. A directory's frame takes its net direction:
`▼ .cache` and `▼ papers` shrank, `▲ datasets` grew. A removed path keeps a
small area, at most a twentieth of the root, so it does not simply vanish from
the picture.*

## Monitor Center (`2`)

A monitor is a saved definition: a path, an interval, a metric, a scan policy
and a retention rule. Monitor Center is where their history is drawn.

There is no daemon. A monitor takes snapshots only while a TUI session is
sampling (`S`) or a `disktide watch` host is running; `enabled/no-host` in the
list means neither is. `Auto-start` makes future TUI sessions start sampling on
their own.

![Monitor Center. The monitor list on the left holds "home  enabled/no-host/unknown". On the right, the home monitor's History tab: a Start sampling button, summary rows reading "34 canonical point(s)", "Pair #33 → #34 · partial confidence", the diff legend with keys, and the trend marks; tabs for Trend, Diff Map, Growth Rings and Heatmap; the Space-Time Trend of home climbing in steps from 1167.7 MiB to 1394.9 MiB between 2026-08-12 and 2026-09-12, with a dip around the start of September; and a history table whose 2026-09-04 row is flagged "partial, pinned". Nine numbered pins mark the parts described below.](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/monitor-trend.png)

*The History tab, on the Trend.*

1. **Monitor list.** Each monitor's state, written desired / activity / health.
2. **`Start sampling [S]`** hosts every enabled monitor in this session.
   **Auto-start** remembers that choice for next time.
3. **Tabs.** History, Details, Alerts and Retention. Click them.
4. **Summary.** First the collection state and the number of points. Then the
   snapshot pair that all four charts draw, and how far to trust it. `b` and
   `v` on a row of the history table set the baseline and the target, and `l`
   goes back to the newest pair.
5. **Keys and marks.** The diff legend and its keys, then what the marks on the
   trend mean: `*` alert or anomaly, `c` cleanup, `~` partial, `o` pinned,
   `.` sampled.
6. **Chart tabs.** `F1` to `F4`, or `Tab` to cycle through them.
7. **Points.** One per snapshot, marked by what kind of snapshot it was.
8. **Axes.** Size in MiB against time. `z` zooms to the newest 50% or 25% of the
   history, and `Shift+←`/`Shift+→` pans.
9. **History table.** One row per snapshot, flagged `partial` or `pinned`. `i`
   pins the highlighted snapshot so that retention never removes it. The one
   pinned here is the last snapshot before the backup tarball was deleted.

The trend plots the root. If a subdirectory was highlighted in the Explorer when
you pressed `2`, that directory becomes a second series (so does `Enter` on a
heatmap row). The two series share one axis, so a small directory next to a
large root draws almost flat.

To see the root's own shape, highlight the root first, as here. The steps are
where CSVs, photos and `node_modules/` landed, the bump is the backup tarball
coming and going, and the dip is the emptied pip cache before its rebuild.

### Diff Map (`F2`) and Growth Rings (`F3`)

<img alt="The Diff Map tab: a treemap of the pair #33 → #34 coloured by direction, with the home frame red, .cache and papers green, and an amber block for downloads."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/monitor-diff-map.png" width="49%">
<img alt="The Growth Rings tab: a tiles-shaped sunburst of the same pair coloured by direction, with &quot;home ≈ ▲ +1.0 MiB (+0.1%)&quot; in the centre."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/monitor-growth-rings.png" width="49%">

*Diff Map and Growth Rings for the pair in the summary.*

These are the Explorer's two diff views, drawn for the pair named in the
summary. Area, or length along the ring, is the target snapshot's size. Colour
is the direction, and removed paths keep a small, bounded area. Growth Rings
puts the root's net change in its centre.

### Heatmap (`F4`)

The heatmap answers a different question from the other three charts. It shows
neither what is biggest nor what changed last, but **what keeps growing**.

![The Heatmap tab: paths on the left, sixteen interval columns in the middle and a consistency column on the right. tmp and tmp/build.log show a row of 1s at 100% s16; downloads and its files show ? marks with one digit at the right end, at 100% s1; projects/thesis reads 75% s5; models shows a digit every third column at 31% s1. A legend row ends in "+N more". Seven numbered pins mark the parts described below.](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/monitor-heatmap.png)

*The Heatmap over the last sixteen intervals.*

1. **Rows** are paths under the monitored root. They are ranked by consistency,
   then by the longest run of growth, then by how much the path grew.
2. **Columns** are the intervals between consecutive snapshots, up to sixteen,
   with the newest on the right.
3. **A digit** is an interval in which the path grew, graded from 1 to 4 against
   the largest change on the map. `tmp/build.log` grew in all sixteen.
4. **`?`** means the path did not exist yet. Everything in `downloads/` appeared
   in the last interval.
5. **`·`** is an interval with no change. `models/` grows every third day.
6. **consistency** is the share of intervals in which the path grew, counted
   only over the intervals where it existed. **`sN`** is its longest run of
   growth. Read the two together: `downloads/` is 100% consistent over a single
   interval (`s1`), while `tmp/build.log` is 100% consistent over sixteen
   (`s16`).
7. **Legend**, and how many paths did not fit. `↑` and `↓` pick a row, and
   `Enter` plots that path as the Trend's second series.

Shrink (`▼`), removed (`×`), partial (`≈`) and incompatible (`!`) cells use the
same marks as Diff. A cell from a partial pair of snapshots is drawn dim, and it
keeps its direction.

### Details and Retention

<img alt="The Details tab of the home monitor: Lifecycle (Desired enabled, Activity no-host, Health unknown, Host: none — enabled is not a background daemon), Schedule (every 1d, last success not in this session), Scan resource, and Watch & confidence."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/monitor-details.png" width="49%">
<img alt="The Retention tab: the policy line &quot;keep all 1d, hourly to 30d, daily to 365d&quot;, a preview of how many snapshots maintenance would keep, prune, roll up and pin, and a table of snapshots with the protection each one gets."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/monitor-retention.png" width="49%">

*Left: Details. Right: Retention.*

**Details** spells out the monitor's lifecycle. Note `Host: none`: enabled does
not mean a daemon is running. The tab also shows its schedule, the scan
resources it gets, and whether it watches for filesystem events or polls.

**Retention** previews what maintenance would do under the monitor's policy.
The default keeps every snapshot for a day, one per hour for 30 days, and one
per day for a year. Within each of those buckets the newest snapshot is kept and
stands in for the rest, which are pruned. Pinned snapshots and the newest few
are always kept. `t` runs maintenance.
Here the preview reads `keep 31, prune 3, rollups 2, pinned 1`. The three
snapshots taken within one hour on 22 August roll up into the newest of them,
the two from the oldest day roll up into one, and the pinned #26 stays.

### Setting one up

![The "Set up monitoring" dialog over the Explorer, with Path filled in as /tmp/home/projects, an optional Label, Interval 6h, Metric "Logical bytes", Retention "Balanced", a "Capture first snapshot" switch turned off, and an "Advanced scan policy" section with Workers, Max depth and One filesystem.](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/monitor-editor.png)

*`M` on `projects/` in the Explorer.*

`M` on a directory in the Explorer, or `n` in Monitor Center, opens this editor
with the path filled in. It takes a label, the interval, the metric, a retention
preset and an advanced scan policy.

*Capture first snapshot* is off by default, so a new monitor has nothing to draw
until something samples it: `S` in Monitor Center, `R` to run it once now, or
`disktide watch`.

## FS Overview (`3`)

FS Overview scans no directories. It lists the machine's mounted filesystems
and block devices: capacity, usage, the kind of storage behind each one, and
your quota. On a shared machine it is the first thing to check before wondering
why a scan is slow or a write failed.

The three figures in this section show a made-up server, not the machine they
were taken on. The screen is the real one, but `tool/gen_blog_shots.py` hands
it a fixed mount table, disk list and quota, so no real host's details end up
in a public post. The server has an encrypted NVMe root, a RAID of spinning
disks with a container runtime's volumes bound out of it, a btrfs backup disk,
NFS home and project shares, a Lustre scratch, a few snap packages, and a new
disk nobody has formatted yet.

![FS Overview of the made-up server. The summary reads "21 filesystem(s) mounted | Total: 34.7 TiB | Used: 20.7 TiB (60%) | 8 read-only image mount(s) | 5 mount(s) of a listed filesystem folded". A capacity bar is split between /, /backup, /data, /home, /projects, /scratch and "10 others". The table lists those mounts with Storage chips such as "Flash · Encrypted", "HDD · CoW · Compressed" and "HDD · RAID", usage bars with /projects at 78.0% and /data at 91.4%, a quota of 38.2 GiB/50.0 GiB on /home, and two folded rows. Below it, the Block Devices tree of an NVMe disk, two RAID members, a USB disk and an unformatted disk, under a warning that one disk has no mounted filesystem. Seven numbered pins mark the parts described below.](https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/fs-overview.png)

*FS Overview of the made-up server.*

1. **Summary.** How many filesystems are mounted, their total and used
   capacity, and how many rows were folded away, with the reason for each pile.
2. **Capacity bar.** Each filesystem gets width in proportion to its capacity,
   coloured by what it is stored on: red for network, orange for spinning
   disks, green for flash or RAM. Filesystems under 1% of the total are summed
   into `others`.
3. **Storage.** The medium as a label, followed by chips for whatever is
   stacked on it: `Encrypted` on the LUKS root, `RAID` on the md array, and
   `CoW · Compressed` on the btrfs backup disk.
4. **Usage.** Used over used-plus-available, the way `df` computes it, so blocks
   reserved for root do not count as free. The bar turns orange at 70%
   (`/projects`) and red at 90% (`/data`). A read-only image gets a plain bar,
   since full is what read-only means.
5. **Quota.** Your usage and your limit, wherever `quota` answers: here the NFS
   home.
6. **Block devices.** `lsblk` as a tree of disks, partitions, RAID members and
   the encrypted volume, and where each is mounted. A disk with no mounted
   filesystem anywhere on it is called out in the section header, like `sdd`
   here.
7. **Folded rows.** Rows that repeat what the table already says fold into one
   line each: the eight snap images, and five container volumes bound out of
   `/data`, each of which reports all of `/data` again. `i` lists them.

<img alt="The details popup for /data: device /dev/md0, ext4, speed Medium (HDD), attributes HDD · RAID, 91.4% used with 372.2 GiB reserved for root, inode counts, a 4 KiB block size and the mount options."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/fs-mount-details.png" width="49%">
<img alt="The details popup for the nvme0n1 disk: type disk, size 1.8 TiB, model NVMe SSD 2TB, SSD / flash media, and its three partitions: the EFI system partition, /boot and the encrypted root."
     src="https://raw.githubusercontent.com/williamqwu/assets/main/disktide/blog/fs-block-details.png" width="49%">

*`Enter` on a mount (left) or on a block device (right).*

`Enter` opens the details of the highlighted row. For a mount that is its
device, type, speed class and storage chips, its capacity including the blocks
reserved for root, your quota where there is one, inodes, block size and mount
options. For a row in the block-device table it is the disk's model, media and
partitions.

`B` benchmarks the highlighted mount. It first names the directory it will write
in, which is your own directory on that filesystem when the mount root is not
writable, and asks again before writing anything. A run writes at most 256 MiB,
and never more than a quarter of the space your quota leaves. On a mount with
nowhere writable, it says so and does nothing.

## Where to go next

- The [user guide](../user-guide.md) covers every key, metric and configuration
  option. In the app, `?` lists the keys of the screen you are on.
- [Reading the sunburst](2026-05-24-reading-the-sunburst.md) is about where
  radial space-filling charts come from, and why they suit a filesystem.
