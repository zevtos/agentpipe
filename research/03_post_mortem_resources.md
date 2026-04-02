# The definitive guide to public post-mortem resources

**The single best starting point is Dan Luu's GitHub collection** (https://github.com/danluu/post-mortems), which indexes hundreds of public post-mortems from major tech companies and links to a dozen other curated lists. But the ecosystem extends far deeper — spanning dedicated company incident blogs, landmark write-ups that changed how the industry thinks about failure, and an entire intellectual tradition rooted in resilience engineering. This guide maps the full landscape of freely available post-mortem resources across curated collections, company archives, famous incidents, and structured learning materials.

---

## Curated collections that aggregate the best post-mortems

The open-source community maintains several overlapping but complementary repositories. **Dan Luu's post-mortems repo** (https://github.com/danluu/post-mortems) remains the gold standard — with **~12,000 GitHub stars**, it catalogs hundreds of incidents organized by failure category (config errors, hardware failures, time bugs, database issues) and links to the original write-ups. Its companion essay at http://danluu.com/postmortem-lessons/ distills cross-cutting patterns. The repo's "Other lists of postmortems" section is itself a meta-index pointing to over a dozen additional collections.

**Lorin Hochstein's major-incidents** repo (https://github.com/lorin/major-incidents) takes a different approach, curating a sample of infrastructure outages primarily sourced from SRE Weekly. Hochstein, a staff SRE formerly at Netflix and Airbnb, also maintains a **resilience-engineering papers list** (https://github.com/lorin/resilience-engineering) with 3,000+ stars — the academic companion to practitioner post-mortems. His blog **Surfing Complexity** (https://surfingcomplexity.blog/) publishes expert "quick takes" analyzing recent public incidents from companies like Cloudflare, OpenAI, and GitHub.

Several "awesome-postmortems" repositories fill different niches. **carlsborg/awesome-postmortems** (https://github.com/carlsborg/awesome-postmortems) stands out for providing deep technical breakdowns alongside each entry. **jimmyl02/awesome-postmortems** (https://github.com/jimmyl02/awesome-postmortems) stays current with recent incidents like OpenAI's December 2024 outage. **Nat Welch's icco/postmortems** (https://github.com/icco/postmortems) adds structured metadata to each entry for searchability. For templates rather than incidents, **dastergon/postmortem-templates** (https://github.com/dastergon/postmortem-templates) collects formats from Google, Elastic, and others.

For ongoing curation, **SRE Weekly** (https://sreweekly.com/) is a long-running newsletter — now at 500+ issues — that includes an "Outages" section linking to recent public incident write-ups each week. The **Learning from Incidents** community (https://www.learningfromincidents.io/) and **Availability Digest** (https://www.availabilitydigest.com/) round out the ecosystem of active aggregators.

---

## Company-by-company guide to public incident archives

Not all companies publish post-mortems equally. Some — Cloudflare, Honeycomb, Slack — write deeply technical, narrative-rich analyses for nearly every significant incident. Others publish only for catastrophic events. Here's where to find them.

**Cloudflare** is the most prolific publisher of public post-mortems in the industry. Their archive at https://blog.cloudflare.com/tag/post-mortem/ covers DNS failures, CDN outages, BGP leaks, and configuration errors with exceptional technical depth — often multiple per quarter. **Honeycomb** (https://www.honeycomb.io/blog/) practices what it preaches about observability, publishing some of the most analytically rich incident write-ups available, including detailed Kafka cluster failures and cache death spirals. **Slack** (https://slack.engineering/) publishes less frequently but its post-mortems — like the memorable "A Terrible, Horrible, No Good, Very Bad Day at Slack" — are exceptionally well-written.

**GitHub** publishes monthly availability reports plus standalone analyses at https://github.blog/, with their October 2018 database failover post-mortem being widely considered a masterclass. **GitLab's** January 2017 database deletion post-mortem (https://about.gitlab.com/blog/postmortem-of-database-outage-of-january-31/) remains one of the most transparent ever written. **Linear** (https://linear.app/) earned praise for its January 2024 data-loss write-up, widely called one of the best post-mortems in recent memory.

For cloud providers, **AWS** publishes Post-Event Summaries at https://aws.amazon.com/premiumsupport/technology/pes/ — about 20+ major incidents dating back to 2011, retained for at least five years. **Google Cloud** posts Post-Incident Reviews accessible through https://status.cloud.google.com/summary. **Microsoft Azure** publishes PIRs at https://azure.status.microsoft/en-us/status/history/, with Azure DevOps maintaining its own postmortem blog at https://devblogs.microsoft.com/devopsservice/?cat=2.

**Meta** publishes only for major events at https://engineering.fb.com/, but their October 2021 outage analysis is essential reading. **Roblox** published a single but extraordinary post-mortem for their 73-hour outage at https://blog.roblox.com/2022/01/roblox-return-to-service-10-28-10-31-2021/. **Atlassian** documented their infamous two-week April 2022 outage with follow-up reports at https://www.atlassian.com/blog/atlassian-engineering/post-incident-review-april-2022-outage. **Fastly's** June 2021 CDN outage analysis lives at https://www.fastly.com/blog/summary-of-june-8-outage.

**PagerDuty** is less a post-mortem publisher and more a post-mortem *enabler* — their open-source incident response documentation at https://response.pagerduty.com/ is an industry-standard guide, and their postmortem process page at https://postmortems.pagerduty.com/culture/blameless/ is one of the best free resources on blameless culture.

---

## Twelve landmark post-mortems every engineer should read

These incidents are famous not just for their severity but for what they teach about complex system failure. Each one illuminates a different failure pattern.

**GitLab's database deletion (January 31, 2017)** — https://about.gitlab.com/blog/postmortem-of-database-outage-of-january-31/ — An engineer ran `rm -rf` on the primary PostgreSQL database instead of the secondary. The 18-hour recovery revealed that **five separate backup mechanisms had all silently failed**. GitLab live-streamed the recovery on YouTube to 5,000+ viewers. The canonical lesson: backups are worthless until tested.

**AWS S3 outage (February 28, 2017)** — https://aws.amazon.com/message/41926/ — A typo in a billing-system debug command removed far more S3 servers than intended in US-EAST-1, cascading into a 4-hour outage that broke a huge swath of the internet. AWS's own status dashboard went down because it depended on S3. Netflix was famously unaffected due to multi-region architecture. The lesson: **circular dependencies between services and their monitoring are a ticking bomb**.

**Cloudflare's regex outage (July 2, 2019)** — https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019/ — A single poorly written regular expression in a WAF rule caused catastrophic backtracking, exhausting CPU across Cloudflare's entire global network for 27 minutes. Led Cloudflare to migrate to Rust's regex engine to eliminate ReDoS risks entirely.

**Cloudbleed (February 2017)** — https://blog.cloudflare.com/incident-report-on-memory-leak-caused-by-cloudflare-parser-bug/ — A single-character bug (`>` instead of `>=`) in a legacy HTML parser leaked passwords, cookies, and encryption keys from customers' requests into other customers' HTTP responses for five months before Google Project Zero caught it. Demonstrates the unique risk profile of CDN/proxy services.

**Facebook/Meta 6-hour global outage (October 4, 2021)** — https://engineering.fb.com/2021/10/05/networking-traffic/outage-details/ — A maintenance command accidentally disconnected all data centers; DNS servers withdrew BGP advertisements as designed, removing Facebook, Instagram, and WhatsApp from the internet. **Recovery was blocked because badge access and internal tools also depended on the downed infrastructure** — engineers had to be physically dispatched to data centers.

**Knight Capital's $440M loss in 45 minutes (August 1, 2012)** — No official post-mortem exists, but the SEC investigation and analyses (e.g., https://www.henricodolfing.ch/en/case-study-4-the-440-million-software-error-at-knight-capital/) are thorough. A deployment script silently failed on one of eight servers, activating decade-old dead code that executed 4 million uncontrolled trades. The canonical example of how **dead code, silent deployment failures, and missing kill switches** compound into catastrophe.

**Roblox 73-hour outage (October 28–31, 2021)** — https://blog.roblox.com/2022/01/roblox-return-to-service-10-28-10-31-2021/ — Two unrelated bugs (Consul streaming contention + a BoltDB performance pathology) interacted unpredictably. Monitoring depended on the failing Consul infrastructure, creating a chicken-and-egg debugging nightmare for 50 million daily users. Estimated **$25M in lost bookings**.

**GitHub's October 2018 outage (24 hours, 11 minutes)** — https://github.blog/2018-10-30-oct21-post-incident-analysis/ — A 43-second network blip triggered automated database failover, creating divergent writes across data centers. Reconciliation took over 24 hours. A textbook demonstration of the CAP theorem in practice and the risks of automated failover.

**CrowdStrike/Windows global IT outage (July 19, 2024)** — A faulty Falcon sensor update caused BSOD crashes on **~8.5 million Windows devices** worldwide. Airlines, hospitals, and banks were paralyzed. The update was reverted in 78 minutes but affected machines required manual Safe Mode intervention. Estimated **$5.4 billion in Fortune 500 losses** alone — called the largest IT outage in history.

**Atlassian's two-week outage (April 2022)** — https://www.atlassian.com/blog/atlassian-engineering/post-incident-review-april-2022-outage — A deletion script received site IDs instead of app IDs, permanently destroying 883 customer sites. The API accepted both ID types without validation. Recovery required manual per-customer restoration from backups over 14 days.

**Fastly CDN outage (June 8, 2021)** — https://www.fastly.com/blog/summary-of-june-8-outage — A customer's routine configuration change triggered a latent bug, taking down 85% of Fastly's network and disrupting Amazon, Reddit, the New York Times, and UK government sites.

**Slack's January 4, 2021 outage** — https://slack.engineering/slacks-outage-on-january-4th-2021/ — AWS Transit Gateway capacity limits combined with a first-day-back-from-holiday traffic surge created cascading failures, illustrating how infrastructure limits and traffic patterns interact.

---

## Books, talks, and resources that shaped post-mortem culture

The intellectual foundations of modern post-mortem practice draw from resilience engineering, cognitive psychology, and safety science — not just software engineering.

**Richard Cook's "How Complex Systems Fail"** (https://how.complexsystems.fail/) is the 18-point treatise that changed how the tech industry thinks about failure. Originally written for healthcare, it argues that **post-accident attribution to a single root cause is fundamentally wrong** and that hindsight bias distorts all post-accident assessments. John Allspaw, then CTO of Etsy, recognized its applicability to web operations in 2009, catalyzing the resilience engineering movement in tech.

**Google's SRE Book** offers the most practical institutional guidance. Chapter 15 on post-mortem culture (https://sre.google/sre-book/postmortem-culture/) covers when to write post-mortems, blameless culture philosophy, and tooling. Appendix D provides an example post-mortem (https://sre.google/sre-book/example-postmortem/). The **SRE Workbook** extends this with Chapter 10 (https://sre.google/workbook/postmortem-culture/), including side-by-side "bad" versus "good" postmortem comparisons. Both are freely available online.

**Etsy's Debriefing Facilitation Guide** (https://www.etsy.com/codeascraft/debriefing-facilitation-guide/ and full PDF at https://extfiles.etsy.com/DebriefingFacilitationGuide.pdf) is the gold standard for facilitation. Authored by Allspaw's team, it emphasizes looking for the "Second Story" behind incidents, asking "what" and "how" questions rather than "why," and resisting the urge to identify a single root cause. Etsy also open-sourced **Morgue** (https://github.com/etsy/morgue), a tool for managing postmortem records.

**Sidney Dekker's books** provide the theoretical backbone. *The Field Guide to Understanding Human Error* reframes "human error" as a symptom of systemic issues rather than a cause. *Just Culture* covers how to respond to incidents without punishing individuals. Both are essential for anyone building post-mortem culture.

For hands-on practice, **Wheel of Misfortune** (https://dastergon.github.io/wheel-of-misfortune/) is an open-source role-playing game inspired by Google's internal SRE training where team members simulate incident response against real scenarios. **PagerDuty's open-source incident response guide** (https://response.pagerduty.com/) provides end-to-end documentation from detection through post-mortem, including their nuanced take on "blame-aware" (rather than purely "blameless") postmortems.

The **Learning from Incidents** community (https://www.learningfromincidents.io/), founded by Nora Jones (Jeli founder, now PagerDuty), connects resilience engineering research with tech practice. Jones, who co-authored O'Reilly's *Chaos Engineering* book, also built **Jeli** (https://www.jeli.io/) — the first dedicated incident analysis platform, which auto-creates timelines from Slack, Zoom, and Jira data. **Adaptive Capacity Labs** (https://www.adaptivecapacitylabs.com/), co-founded by Allspaw, Cook, and David Woods, brings decades of safety science to tech incident analysis.

Key conference venues for post-mortem content include **SREcon** (USENIX), **REdeploy** (focused on resilience), **LeadDev**, and **DevOpsDays**. Gergely Orosz's **Pragmatic Engineer** guide (https://blog.pragmaticengineer.com/postmortem-best-practices/) synthesizes how 50+ teams handle incidents and references the **Verica Open Incident Database (VOID)** as the most exhaustive public incidents dataset available.

---

## Conclusion

The post-mortem landscape has matured from scattered blog posts into a rich, interconnected ecosystem. Start with **Dan Luu's collection** for breadth, read the **twelve landmark incidents** above for pattern recognition, and ground your practice in **Cook's "How Complex Systems Fail"** and Google's SRE chapters for philosophy and process. The most important recurring insight across all these resources is that complex failures are never monocausal — and the organizations that learn fastest are those that resist the comfort of simple explanations. The shift from "blameless" to "blame-aware" postmortems, championed by practitioners like Allspaw and Jones, reflects a maturing understanding that psychological safety is not a policy to declare but a culture to continuously build.