# Poster / visual design

Use this checklist for posters, flyers, covers, banners and other print or
screen visuals.

## Brief and acceptance criteria
- [ ] Restate the audience, purpose, format, finished trim size, orientation,
      quantity, viewing distance and where the piece will be seen.
- [ ] List every required fact: headline, subtitle, body, date, time and time
      zone, venue, price and currency, call to action, sponsor/legal text and
      QR destination.
- [ ] Turn the brief into numbered acceptance criteria (`R1...Rn`) with a
      measurable pass/fail test and an evidence file for each.
- [ ] Rank the content: one primary message, two or three supporting points,
      everything else small. Record any unresolved question as an assumption.
- [ ] Pick one visual concept and a small reference set; do not mix unrelated
      styles.
- [ ] Define the output matrix up front: print PDF, screen PNG, social crops,
      and the size and colour mode of each.

## Hierarchy and typography
- [ ] One focal point and an obvious reading order: headline, supporting
      message, details, call to action.
- [ ] Brand marks use approved files and colours; preserve clear space, aspect
      ratio and minimum size, and never stretch, recolour or add effects.
- [ ] Body text at least 10 pt at final print size; absolute minimum 7 pt for
      legal text. Headline is legible from the stated viewing distance.
- [ ] No more than two type families and three weights; consistent scale,
      leading and tracking. Avoid long passages set in all caps.
- [ ] Contrast ratio at least 4.5:1 for body text and 3:1 for large text;
      information is never carried by colour alone.
- [ ] Keep all text at least 5 mm inside the trim. Nothing important enters the
      bleed.
- [ ] Check the actual rendered size at 100% and as a thumbnail; a squint test
      must still reveal the intended hierarchy.

## Headless Chrome and long commands
- [ ] Every headless Chrome, browser, server or network command runs under a
      hard shell timeout, e.g. `timeout 60 chrome --headless=new ...
      --screenshot=...`. If `timeout` is not installed (default macOS), use
      `gtimeout 60 ...` or `perl -e 'alarm 60; exec @ARGV' -- ...`. Do not rely
      on the tool's own timeout.
- [ ] Use a fresh `--user-data-dir` inside the workspace and set `HOME` and
      `TMPDIR` inside the workspace; Chrome cannot use the machine's
      Application Support or temp directory under the sandbox.
- [ ] Kill leftover Chrome processes for that profile and remove the profile
      when the command finishes.
- [ ] Use only the browser binary from the connector render probe (or one
      already on PATH). Never download a browser or execute a binary from the
      workspace's `.tools` folder; the workspace is model-writable.
- [ ] Make at most two Chrome attempts per render. If both fail, stop, keep the
      HTML source, and report `render unavailable: <reason>`; never loop on
      browser variants or profiles.

## Layout, images and print production
- [ ] Render at the exact target size with headless Chrome (under the timeout
      rules above) and inspect the screenshot; no overflow, clipping, cut-off
      words or collisions.
- [ ] No overlapping text, text over busy image areas, or labels that collide.
- [ ] Use one grid with consistent gaps and intentional alignment. No large
      empty area that makes the design look unfinished.
- [ ] Every image is relevant and licensed. Require at least 300 effective PPI
      at the placed size (150 PPI for large format); reject upscaled images and
      record attribution.
- [ ] Print: set 3 mm bleed and crop marks unless the printer specifies
      otherwise; use CMYK with the printer's profile, rich black only for large
      areas, 100K for small text, total ink within the printer's limit, and
      lines no thinner than 0.25 pt.
- [ ] Embed or outline fonts; export PDF/X-4 (or the printer's standard) and
      run preflight. Check the exported PDF, not only the browser view.
- [ ] QR codes (if any) have a 4-module quiet zone, error correction M or
      higher, at least 20 mm square at close range (larger for the stated
      scanning distance), strong contrast, and decode from the final exported
      file. The encoded URL is the production destination, not a staging page.
- [ ] Screen: use sRGB, correct pixel dimensions, a file-size cap and safe
      zones for platform overlays.

## Content, variants and proofing
- [ ] Every required fact appears correctly, is spelled correctly and does not
      conflict with a duplicate elsewhere.
- [ ] Dates, times, prices, URLs and names checked against the brief. Legal,
      sponsor and accessibility text are present where required.
- [ ] The call to action is obvious and singular; a reader knows what to do
      next.
- [ ] Produce the full variant matrix (print, screen, social) and give every
      digital variant alt text that conveys its message.
- [ ] Proof at 100%, as a thumbnail, in grayscale and with a colour-blindness
      simulation. Compare every fact against the brief one last time.
- [ ] Export includes a print-ready PDF/PNG at the required DPI, the editable
      source, and a preflight or export report.

## Visual impact

- [ ] Choose 2-3 genuinely distinct directions before building; do not submit
      several variants of one safe layout.
- [ ] State the focal point, 2-3 colour system, type pairing and single
      image/graphic device for the chosen direction.
- [ ] Avoid the default-template look (centered headline over a stock hero, one
      flat colour block, generic sans). Use a deliberate focal point, layered
      composition and generous negative space.
- [ ] Inspect the final render at target size and at phone scale with
      `read_image`. Ask whether it would stop a scroll and whether it looks
      designed for this brand rather than generated.
- [ ] Record `Visual impact: X/10` and name the strongest and weakest visual
      element. An accurate but plain poster must not pass as finished work.

## Review and quality gate
- [ ] Compare at least two independent concepts or layouts, and record why the
      chosen direction is stronger.
- [ ] Use a reviewer that did not create the poster. It inspects the rendered
      PDF/PNG with `read_image` at 100% and as a thumbnail, verifies the QR code,
      and scores the result 0-10 against this checklist.
- [ ] Blocking defects include a missing or wrong fact, clipped or overlapping
      text, type below the minimum, a QR that fails to decode, the wrong trim or
      bleed, and an unlicensed or visibly low-resolution image.
- [ ] Fix every blocking defect; revise until the score is at least 8.5 with
      zero blocking defects, or three rounds have been used. If the quality
      loop is disabled, run the same checks yourself and record that
      independence was not possible.
- [ ] When the quality loop is enabled, save `QUALITY_REPORT.md` with the
      numbered checklist, per-round scores, defects found and fixed, evidence
      paths (screenshot, PDF, preflight, QR decode) and the final score;
      otherwise include the same checklist, score and evidence in the final
      answer. In both cases end the answer with `Final quality score: X/10`.

## Lessons learned
<!-- Add only verified, reusable lessons as symptom -> cause -> check to add.
     Keep them short and free of names, emails, tokens and personal paths. -->
