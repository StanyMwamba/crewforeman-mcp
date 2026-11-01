# Web page

Use this checklist for landing pages, portfolios, documentation sites and other
browser deliverables.

## Brief and acceptance criteria
- [ ] Restate the audience, page goal, required sections, primary call to
      action, content source, browser support and hosting target.
- [ ] Turn the brief into numbered acceptance criteria (`R1...Rn`) with a
      measurable pass/fail test and an evidence file for each.
- [ ] Inventory the copy, images, links and forms before building; record
      missing inputs as assumptions to confirm.
- [ ] Set a performance and accessibility budget up front (for example
      Lighthouse performance and accessibility >= 90, or explicit LCP, CLS and
      INP targets).

## Document shell and content
- [ ] Valid `<!doctype html>`, correct `lang`, `charset` and responsive
      viewport meta.
- [ ] Unique page title, meta description, favicon, canonical URL, and Open
      Graph/Twitter tags where the page will be shared.
- [ ] Real copy is used; no lorem ipsum, empty placeholder cards or dead
      `href="#"` links. Every required fact, link and call to action is present.
- [ ] One `h1`, semantic landmarks (`header`, `nav`, `main`, `footer`) and a
      logical heading order.
- [ ] Meaningful link text; contact, legal/privacy and error/404 paths exist
      where the brief requires them.
- [ ] Images have `width`/`height` or `aspect-ratio` to avoid layout shift.

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
- [ ] Make at most two Chrome attempts per render. Batch all required viewport
      widths into one command; the batch counts as one attempt. If both attempts
      fail, stop, keep the HTML page, and report `render unavailable: <reason>`;
      never loop on browser variants or profiles.

## Layout and responsiveness
- [ ] Render at 320/360, 390, 768, 1024 and 1440 px widths with headless
      Chrome (under the timeout rules above), plus 200% zoom and landscape
      orientation.
- [ ] No horizontal scroll, clipped content, overlapping elements or fixed
      widths that break small screens.
- [ ] Tap targets at least 44 px with adequate spacing; the mobile navigation
      is usable by touch and keyboard.
- [ ] Fluid grids and images (`srcset`/`sizes`) behave at every breakpoint;
      sticky headers do not cover in-page anchors.
- [ ] No large empty regions, unstyled default components or layout shift
      caused by late-loading fonts or media.

## Accessibility
- [ ] Target WCAG 2.2 AA. Text contrast at least 4.5:1 (3:1 for large text);
      information is not conveyed by colour alone.
- [ ] Visible focus states, logical focus order, a skip link and full keyboard
      operation, including menus, dialogs and forms.
- [ ] Form fields have persistent labels, clear errors tied to the field and
      appropriate `autocomplete`; no placeholder-only labels.
- [ ] Alt text conveys the purpose of meaningful images; decorative images are
      ignored by assistive technology.
- [ ] Respect `prefers-reduced-motion`, do not autoplay media, and keep
      animation from blocking reading or interaction.
- [ ] ARIA is used only when native semantics are insufficient; names, roles
      and states are exposed correctly to a screen reader.

## Correctness, security and performance
- [ ] All internal links, anchors and local assets resolve; there are no
      console errors and no 404s in the network panel.
- [ ] HTML validates and CSS parses without errors.
- [ ] No secrets or personal data in source. External new-tab links use
      `rel="noopener"`; untrusted HTML is never rendered unescaped.
- [ ] Forms validate on the client for usability and on the server for safety
      where a backend exists; never trust client-side validation alone.
- [ ] Images use modern formats and responsive sizes; below-the-fold media is
      lazy-loaded; fonts use `font-display`; no megabyte-scale library without a
      documented reason.
- [ ] The page meets the agreed budget and works offline if the brief requires
      it; no request depends on a missing local file.

## Verification and evidence
- [ ] Save screenshots at 320/768/1440 px and record the browser and version
      tested.
- [ ] Run HTML validation, an axe or Lighthouse accessibility check, and a
      link/anchor crawl; save the reports.
- [ ] Tab through every interactive element and submit each form once; record
      the result.
- [ ] Write a short test note covering viewports, browser, checks run and any
      known limitation.

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
      element. An accurate but plain page must not pass as finished work.

## Review and quality gate
- [ ] Compare at least two independent layout or content structures, and record
      why the chosen one is stronger.
- [ ] Use a reviewer that did not build the page. It inspects mobile and
      desktop screenshots with `read_image`, tab-tests the page, checks every
      CTA and anchor, and scores the result 0-10 against this checklist.
- [ ] Blocking defects include horizontal scroll, a broken navigation or CTA,
      an unusable form, a contrast failure, a console error or missing asset,
      missing required content, and layout shift that moves the primary action.
- [ ] Fix every blocking defect; revise until the score is at least 8.5 with
      zero blocking defects, or three rounds have been used. If the quality
      loop is disabled, run the same checks yourself and record that
      independence was not possible.
- [ ] When the quality loop is enabled, save `QUALITY_REPORT.md` with the
      numbered checklist, per-round scores, defects found and fixed, evidence
      paths and the final score; otherwise include the same checklist, score
      and evidence in the final answer. In both cases end the answer with
      `Final quality score: X/10`.

## Lessons learned
<!-- Add only verified, reusable lessons as symptom -> cause -> check to add.
     Keep them short and free of names, emails, tokens and personal paths. -->
