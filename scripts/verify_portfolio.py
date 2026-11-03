"""Independent checks on the generated portfolio (reads raw + ground truth)."""
import sys, glob, json
import pandas as pd, numpy as np
root = sys.argv[1]
D = pd.read_csv(f"{root}/ground_truth/deals.csv"); C = pd.read_csv(f"{root}/ground_truth/cashflows.csv")
ok = True
# 1. per-deal ledger identity: RTR = net debits + internal payoff + remaining balance (+ forgiven/discount for some outcomes)
net = C[C.kind.isin(["debit","return"])].groupby("deal_id").amount.sum()
ipo = C[C.kind=="internal_payoff"].groupby("deal_id").amount.sum()
D["net_debits"] = D.deal_id.map(net).fillna(0); D["ipo"] = D.deal_id.map(ipo).fillna(0)
for outc, cond in {"paid_in_full": lambda r: abs(r.net_debits - r.rtr) < 0.02,
                   "renewed_healthy": lambda r: abs(r.net_debits + r.ipo - r.rtr) < 0.02,
                   "refinanced_distressed": lambda r: abs(r.net_debits + r.ipo - r.rtr) < 0.02,
                   "active": lambda r: abs(r.net_debits + r.balance_asof - r.rtr) < 0.02,
                   "active_modified": lambda r: abs(r.net_debits + r.balance_asof - r.rtr) < 0.02}.items():
    sub = D[D.outcome==outc]; bad = sub[~sub.apply(cond, axis=1)]
    print(f"identity {outc:24s} n={len(sub):4d} violations={len(bad)}"); ok &= len(bad)==0
# 2. chain integrity: each renewal's internal payoff equals prior deal's internal_payoff_out
ch = D[D.prior_deal_id.notna()].merge(D[["deal_id","internal_payoff_out"]], left_on="prior_deal_id", right_on="deal_id", suffixes=("","_prior"))
v = (abs(ch.internal_payoff_of_prior - ch.internal_payoff_out_prior) > 0.02).sum(); print("chain payoff mismatches", v); ok &= v==0
# 3. raw ACH, deduped by all columns, reproduces truth cash events (count and sum) except misposting moves deal ids
A = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(f"{root}/raw/ach_exports/*.csv"))])
n_raw = len(A); A["Trans Date"] = pd.to_datetime(A["Trans Date"], format="mixed"); A = A.drop_duplicates(); print("ach rows raw", n_raw, "deduped", len(A))
T = C[C.kind!="commission_out"]
print("truth events", len(T), "sum truth", round(T.amount.sum(),2), "sum ach", round(A.Amount.sum(),2))
ok &= len(A)==len(T) and abs(T.amount.sum()-A.Amount.sum())<0.05
# 4. naive vs adjusted collection from RAW (no ground truth): what a spreadsheet analyst would compute
paid = A[A.Type.isin(["DEBIT","RETURN","PAYOFF","SETTLEMENT"])].Amount.sum(); cashonly = A[A.Type.isin(["DEBIT","RETURN","SETTLEMENT"])].Amount.sum()
print("raw: share of inflows typed PAYOFF", round(A[A.Type=="PAYOFF"].Amount.sum()/paid,4))
# 5. learnability: logistic regression on origination features -> economic bad (closed, funded <= 2025-06)
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
D["fd"] = pd.to_datetime(D.funding_date)
M = D[D.is_closed]
X = pd.get_dummies(M[["fico","tib_months","positions_at_funding","nsf_90d","burden","industry"]], columns=["industry"], drop_first=True).astype(float)
X["log_tib"] = np.log(X.tib_months.clip(lower=3)); X["fico_lt600"] = (M.fico<600).astype(float); X["is_renewal"] = (M.seq_in_chain>1).astype(float)
X["burden_x_refi_proxy"] = 0
y = M.deal_bad_economic.astype(int)
tr = M.fd < "2025-01-01"; te = (M.fd >= "2025-01-01")
clf = LogisticRegression(max_iter=2000).fit(X[tr], y[tr]); auc = roc_auc_score(y[te], clf.predict_proba(X[te])[:,1])
print("closed deals", len(M), "train", tr.sum(), "test", te.sum(), "bad rate train/test", round(y[tr].mean(),3), round(y[te].mean(),3), "temporal AUC", round(auc,3))
print("ALL OK" if ok else "FAIL")
