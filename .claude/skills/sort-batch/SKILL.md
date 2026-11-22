---
name: sort-batch
description: Sort the bird pipeline's exported batch into Instagram posts — identify every species, pick the best frames, group them into posts of at most 10, crop each one, and flag what Scott should check before approving. Use when a batch is on the posting step, when asked to "organize/sort the posts" or "do the next set of exports", or when launched headless after an export.
---

# Sort a batch into posts

Scott photographs birds around North Andover, MA, for @birdsofnorthandover. After
Lightroom exports a batch, this skill does the sorting he used to do by hand: species
IDs, which frames, how they group into carousels, and the crop. **He approves every post
himself later — never post, schedule, or delete anything.**

You may be running headless overnight with nobody to answer. **Never stop to ask.**
Anything you're unsure of becomes a `flag` on that photo (he sees it at review) and a
line in your summary.

All commands run from the repo root with the sorter's venv. Type them exactly like
this — no `cd`, no shell variables, no pipes — because headless runs are only
permitted to run this one script:

```bash
~/.bird_sort_venv/bin/python bird_sort.py prepare           # analyse; prints work dir + sheets
~/.bird_sort_venv/bin/python bird_sort.py grid 0617 0525    # frame + coordinate grid
~/.bird_sort_venv/bin/python bird_sort.py grid 0617=0.42,0.43,0.11,0.11   # check a placed box
~/.bird_sort_venv/bin/python bird_sort.py zoom 0333 0296    # full-res close-ups for field marks
~/.bird_sort_venv/bin/python bird_sort.py preview <work>/plan.json
~/.bird_sort_venv/bin/python bird_sort.py apply <work>/plan.json
```

`<work>` is `~/.bird_sort/<batch folder>/` (prepare prints it). The plan format is in
the docstring at the top of `bird_sort.py`.

## 1. Prepare and look at everything

Run `prepare`. If it says the app isn't on the posting step, stop and say so.

Read **every** `overview_*.jpg` (whole frames; yellow box = the subject it will crop
around, thin cyan = every raw detection with its score; `BEST` = sharpest frame of its
burst) and **every** `birds_*.jpg` (the bird itself, bigger, with a sharpness score `s`).
The printed table lists bursts (frames <2 s apart), sharpness, bird size and light.

**The detector misses small birds.** It has boxed dead leaves and a branch knot while the
warbler sat right beside them. For every photo you might use, check the yellow box is on
the bird. If it isn't (or says NO BIRD), run `grid` on it and read the bird's box off the
0.1-spaced lines as `"bird": [x, y, w, h]` (fractions of the frame, top-left origin).
Be generous by a few percent — a box that clips the tail clips the crop. Then run
`grid 0617=x,y,w,h` to see your box drawn in green and get its real sharpness: the
score on the sheets belongs to the detector's wrong box.

**Sharpness scores only compare like with like** — frames of one burst or one scene. A
bird on snow or against sky scores low because the background is smooth, not because
the bird is soft. Judge softness by eye (zoom), never by the number alone.

## 2. Identify

- Zoom anything that isn't instantly obvious. Name the field marks to yourself before
  naming the bird; small, backlit or fall-plumage birds are where mistakes happen.
- Prior: `docs/species_history.json` lists what he has posted before (ignore the `⚠️`
  prefixes and location suffixes). Something not on that list isn't wrong, but wants a
  closer look and a flag saying why you're confident.
- The `species` field is exactly what the caption shows: the common name (`Red-tailed
  Hawk`), no commas (the caption splits on them), no age/sex — put "juvenile", "female",
  "two birds" in the flag or summary. Plural only when every photo of it shows several.
- Hard pairs that need evidence before you commit: Cooper's vs Sharp-shinned Hawk (tail
  tip shape, head size), fall Blackpoll vs Pine vs Bay-breasted Warbler (feet, streaks,
  face), gulls by age, female/fall warblers generally. If the evidence isn't in the
  frame, flag it with both candidates.
- Too small, blurry or hidden to name → exclude it and say so; never guess a name.

## 3. Choose frames

- Per burst, start from the `BEST` frame; prefer another only when its pose, eye or
  composition is clearly better.
- Drop: soft or motion-blurred bird, eye hidden, bird cut off by the frame, heavy
  branch/leaf cover, near-duplicates of a frame you kept, specks in the sky, birds that
  are pure silhouette against a bright sky (unless the shape itself is striking).
- Keep variety: different poses and behaviours (flight, feeding, calling, a pair) beat
  ten copies of the same perch.
- `duplicate_exports` in `analysis.json` are re-exports of one RAW — keep the newer `-2`
  copy, exclude the other.

## 4. Group into posts

- One species per post when it has about 5+ strong frames. Otherwise combine species
  into themed posts (same habitat or kind of bird), each species' photos together, in
  the order they should be listed.
- At most 10 photos per post. **The first photo is the cover** and Instagram crops the
  whole carousel to its shape — lead with the strongest frame.
- Order posts strongest first. A single weak photo doesn't earn a post; fold it into a
  themed one or exclude it.

## 5. Crop

`bird_sort.py` frames loosely by default — Scott found tighter crops "too tight" twice.
Per photo you set:
- `facing`: `left`/`right` = where the bird looks, so the crop leaves room ahead.
- `crop`: `1` default; `1.2`–`1.4` to keep context worth seeing (nest hole, flower,
  water); `0.8` only for a bird that reads as a speck; `"none"` rarely.

## 6. Location

Default is the batch's (`analysis.json` → `location`, usually `Rea St.`). If a photo's
habitat clearly doesn't match (open water, beach, pier, mountains), keep the default
but flag `location?` — he fills locations in at review.

## 7. Preview, fix, apply

Write `<work>/plan.json`, run `preview`, and **read every `preview_post*.jpg`**. Fix
anything wrong — beak, tail or wingtips cut, bird jammed against an edge, wrong bird
boxed — by editing the plan (`bird`, `facing`, `crop`) and previewing again.

Then `apply`. If it refuses because the batch already has a sorting it didn't write,
**stop**: that's Scott's own work. Don't `--force` unless he asked for a re-sort.

## 8. Report

End with a short summary for Scott (it's also saved as `<work>/report.md`):
a table of posts (species, photo numbers best first), each flag with its reason, and
the excluded count by reason. Plain words, no jargon; photo numbers like `0617`.
