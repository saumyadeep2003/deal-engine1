# Vercel frontend (live engine)

Deploy with Root Directory = `frontend`. `vercel.json` proxies `/api/*` to the Render backend:

    https://deal-engine-dsnp.onrender.com

If that is not your backend URL, re-run

    python scripts/build_frontend.py --backend https://YOUR-SERVICE.onrender.com

and push. `/snapshot` serves the static baked demo as a fallback.
