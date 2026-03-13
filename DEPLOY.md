# Deploy Commence Demo — 5 Minutes to Live URL

## What you're deploying
Two live web apps accessible at public URLs. No server, no install, no IT.
- **App 1:** AI Denials Prediction Dashboard
- **App 2:** SC/SA Revenue Utilization Review

---

## Step 1 — Create free accounts (skip if you have them)
1. **GitHub:** https://github.com/signup — free, takes 2 minutes
2. **Streamlit Cloud:** https://share.streamlit.io — sign in with your GitHub account

---

## Step 2 — Upload code to GitHub
1. Go to https://github.com/new
2. Repository name: `commence-va-demo`
3. Set to **Public**
4. Click **Create repository**
5. On the next screen click **uploading an existing file**
6. Drag the entire contents of the `commence_demo` folder into the upload area
   - Make sure the `data/` folder and `.streamlit/` folder are included
7. Click **Commit changes**

---

## Step 3 — Deploy App 1 (AI Denials)
1. Go to https://share.streamlit.io
2. Click **New app**
3. Select your `commence-va-demo` repository
4. Branch: `main`
5. Main file path: `01_AI_Denials_Prediction.py`
6. Click **Deploy**
7. In ~60 seconds you get a URL like: `https://[yourname]-commence-va-demo-01-ai-denials.streamlit.app`

---

## Step 4 — Deploy App 2 (SC/SA RUR)
1. Click **New app** again
2. Same repository
3. Main file path: `pages/02_SC_SA_RUR.py`
4. Click **Deploy**
5. URL like: `https://[yourname]-commence-va-demo-02-scsa.streamlit.app`

---

## That's it. Share the two URLs.

Anyone with the URL opens it in any browser.
No login. No install. Works on phone, tablet, laptop.

---

## What the apps do at runtime
- Connect to HAPI FHIR R4 public server (hapi.fhir.org) — same spec as Cerner Millennium
- Pull real FHIR Patient, Condition, ExplanationOfBenefit resources
- Score claims against real CMS CARC/RARC denial pattern data (bundled in /data/)
- Match veteran ICD-10 codes against real CFR 38 VASRD SC condition table (bundled in /data/)
- If FHIR server is slow, auto-fall back to pre-loaded synthetic VA patient data
- All human review actions logged with timestamp (FISMA-style audit trail)

## Architecture (one sentence for VA evaluators)
> "What you see is identical to our production architecture — the only difference is the FHIR
> endpoint URL. Point it at your Millennium instance and it's live."

---

## Troubleshooting
- **"Module not found"** → Check that requirements.txt is in the root of the repo
- **Slow load** → HAPI FHIR public server is shared; app auto-falls back to offline data in 8 seconds
- **App crashes** → Check that the `data/` folder with the three JSON files deployed correctly

---

*Commence · Sol. 36C10X26Q0085 · VA RO/CPAC Industry Day · May 19, 2026*
