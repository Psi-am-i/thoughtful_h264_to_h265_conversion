# VTC — Payments / "Buy me a coffee" to-do

Goal: a low-friction tip jar for Very Thoughtful Compression — one link that works
in-app (About panel) and on the web landing page. Not revenue; beer money that says
"this was useful."

## Decision up front

You already have **Yoco**, so the MVP is basically free to stand up. The one real
trade-off to accept or reject:

- **Yoco Payment Link** — a US/EU tipper sees the amount in **ZAR** (e.g. "R55"),
  not "$3". Their card still works (Yoco takes international Visa/Mastercard); their
  bank does the forex. Slightly less slick than a "$3 coffee" button, but zero new
  accounts, ~3% fee, settles to your bank in a day or two.
- **Ko-fi / Buy-me-a-coffee → PayPal → FNB** — shows "$3", better brand fit for an
  international audience, but needs a PayPal account linked to FNB and eats ~8–10%
  per tip in fees + forex.

**Recommendation:** ship the Yoco link now (it's done in 20 minutes), and only add
Ko-fi later *if* the ZAR display actually costs you tips. Don't build two payment
rails for a tip jar before you've seen a single tip.

---

## Phase 1 — MVP (Yoco, this week)  ·  owner: Simon

- [ ] In the Yoco dashboard, create a **Payment Link / Payment Page**
      - [ ] Set it to **customer-chooses-amount** (open amount), or offer 3 presets
            (e.g. R30 / R60 / R120 ≈ small / medium / large coffee)
      - [ ] Name it something honest: "Buy the VTC dev a coffee"
      - [ ] Confirm it settles to the bank account you want
- [ ] Copy the link URL and send it to me
- [ ] Send a screenshot of the Yoco payment page (I'll match the landing-page styling)

## Phase 1 — wiring  ·  owner: me (once I have the link)

- [ ] Add a quiet **"Buy me a coffee"** link in the app's **About panel footer**
      (opens the Yoco link in the user's browser — no in-app payment handling)
- [ ] Same link on the web landing page (Phase 3 below)
- [ ] Copy: one honest line, no guilt-trip ("VTC is free. If it saved you a
      weekend of disk-shuffling, you can buy me a coffee.")

## Phase 2 — international polish (OPTIONAL, only if ZAR display costs tips)

- [ ] Create a **Ko-fi** page (0% platform fee on donations)
- [ ] Create a **PayPal Business** account
- [ ] Link PayPal ↔ **FNB**: FNB Online Banking → *PayPal Services* → register/link
      (FNB is PayPal's official SA partner — this is the only clean SA withdrawal path)
- [ ] Set Ko-fi payout = PayPal
- [ ] Add the Ko-fi link alongside/instead of Yoco for the international audience

## Phase 3 — where the button lives  ·  ties into distribution

- [ ] Landing page (Cloudflare Pages / Netlify): app description + screenshots +
      Mac & Windows download links + the coffee link
- [ ] Decide: single coffee link, or "🇿🇦 pay in ZAR (Yoco) / 🌍 pay in USD (Ko-fi)"
      once both exist

---

## Notes / open questions

- Yoco online/link fee is roughly **~3%** per transaction, no monthly fee — confirm
  the current rate in your dashboard.
- **Ownership: personal.** The tip jar is Simon's, presented under the **Picnic Labs**
  banner (part of "Picnic" — whysoserious.club, whysoserious.city, Picnic Labs — a
  friends' project, not a registered entity). So on the finance side it's **entirely
  personal**: Simon's own Yoco / bank account, personal tax treatment. Nothing here
  touches ScarabTech. Branding can say "Picnic Labs"; the money is Simon's.
- Keep it recoverable-simple: no subscriptions, no memberships, no "goals" bar. One
  button, one link.
