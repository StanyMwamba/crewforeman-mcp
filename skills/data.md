# Data analysis

Use this checklist for datasets, metrics, charts and analytical reports.

## Question and acceptance criteria
- [ ] Restate the decision the analysis supports, the audience, the required
      outputs and how success will be judged.
- [ ] Turn the brief into numbered acceptance criteria (`R1...Rn`) with a
      measurable pass/fail test and an evidence file for each.
- [ ] Define every metric, population, denominator and time window before
      looking at the results.
- [ ] Record assumptions and unresolved questions; confirm anything that would
      change the conclusion.

## Data provenance and privacy
- [ ] Identify the dataset, owner, source, licence, collection method,
      coverage and freshness; record the retrieval date and version.
- [ ] Identify personal or sensitive fields and the legal basis or consent for
      using them. Aggregate or remove identifiers before output; no personal
      data in charts, logs, notebooks or the repository.
- [ ] Check redistribution rights before shipping raw or derived data.
- [ ] Publish a data dictionary: field name, type, unit, allowed range,
      nullability and meaning.

## Validation and preparation
- [ ] Check row and column counts, types, units, time zones, currencies and
      date ranges against the data dictionary.
- [ ] Check primary and foreign keys, duplicates and referential integrity;
      reconcile row counts at every join, filter and aggregation.
- [ ] Look for missing values, impossible values and outliers. State the
      missingness mechanism and handling rule, report how many rows each rule
      affects, and justify any removal or winsorisation.
- [ ] Keep raw data immutable; write cleaning steps to a script and apply them
      consistently. No manual spreadsheet edits in the final pipeline.
- [ ] Save a short data-quality note with the counts and decisions above.

## Method and reproducibility
- [ ] Make the analysis scripted end to end; one documented command rebuilds
      every table, chart and headline number.
- [ ] Pin the environment and package versions, seed all randomness, and record
      filters and date ranges in the code or a run manifest.
- [ ] Report sample size and uncertainty (confidence interval, standard error
      or a stated caveat) for every estimate that supports a conclusion.
- [ ] Include a baseline or comparison, an effect size and a judgement about
      practical significance, not only a p-value.
- [ ] Control multiple comparisons where relevant; check confounding, selection
      bias, survivorship bias and regression to the mean.
- [ ] Do not present correlation as causation. State the causal assumption
      explicitly if a causal claim is required.
- [ ] For predictive work, split training/test data before any fitting and
      check for target leakage; report the metric on held-out data.

## Charts and reporting
- [ ] Every chart has a title, labelled axes, units and a readable scale; the
      chart type fits the comparison being made.
- [ ] Bars start at zero; no truncated axes, dual axes, 3D effects or area
      encodings that exaggerate differences.
- [ ] Show uncertainty where it matters and explain it in the caption or notes.
- [ ] Colours are distinguishable, colour-blind-safe and explained by a legend
      or direct labels; do not rely on colour alone.
- [ ] Remove chartjunk; label the source, date range and any transformation in
      a note.
- [ ] Key numbers in the text match the output tables exactly; state
      denominators, rounding and the sample each number is based on.
- [ ] The report states limitations, alternative explanations and what evidence
      would change the conclusion. It ends with a recommendation that answers
      the original decision, not just a description of the data.
- [ ] Every chart has alt text or a data-table equivalent for accessibility.
- [ ] For a dashboard, show the last-updated time, active filters, population
      and denominator; ensure every card and chart responds consistently, and
      provide loading, empty and error states.

## Delivery
- [ ] Save raw data, processed data, scripts, output tables and charts together
      with a README that gives the exact run command and environment.
- [ ] Execute notebooks top-to-bottom before delivery; ensure no hidden state,
      absolute paths or machine-specific configuration is required.
- [ ] A clean run reproduces the same results; record the row counts and output
      checks used to verify it.

## Review and quality gate
- [ ] Compare at least two independent analysis approaches or model
      specifications, and record why the chosen one fits the decision best.
- [ ] Use a reviewer that did not perform the analysis. It re-runs the pipeline
      from a clean state, spot-checks numbers against the outputs, inspects the
      charts with `read_image`, and scores the result 0-10 against this
      checklist.
- [ ] Blocking defects include unreproducible results, a wrong denominator or
      population, an unsupported or causal claim, a misleading chart, data loss
      and any PII leak.
- [ ] Fix every blocking defect; revise until the score is at least 8.5 with
      zero blocking defects, or three rounds have been used. If the quality
      loop is disabled, run the same checks yourself and record that
      independence was not possible.
- [ ] When the quality loop is enabled, save `QUALITY_REPORT.md` with the
      numbered checklist, per-round scores, defects found and fixed, evidence
      paths (run log, tables, chart images) and the final score; otherwise
      include the same checklist, score and evidence in the final answer. In
      both cases end the answer with `Final quality score: X/10`.

## Lessons learned
<!-- Add only verified, reusable lessons as symptom -> cause -> check to add.
     Keep them short and free of names, emails, tokens and personal paths. -->
