# SOP: Reimburse an employee's expenses

1. Identify the employee (id, manager) in `employees.csv` / the portal. If ambiguous, ask.
2. Find the receipts for the requested period in `docs/receipts/`. List them; do not guess.
3. For each receipt, extract: date, category, amount, currency, passenger/payer name.
4. Check each item against `expense_policy.md`: receipt present, within 60 days, category limit,
   name matches. Cap amounts at the category limit.
5. Check the portal for existing claims for the same employee and date to avoid duplicates.
6. Create a DRAFT claim per reimbursable item (use `client_ref` = `<run_id>:<step_id>`).
7. Request human approval, then submit each draft claim. Submission is final.
8. Verify in the portal that each claim exists with status `submitted` and the right amount.
9. Report: what was claimed, what was capped or skipped and why, claim ids, evidence.
