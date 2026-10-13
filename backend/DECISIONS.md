1. **Indexes.** A unique `employees.emp_code` index protects employee creation and supports employee lookups; `(department, emp_code)` supports sorted employee lists. The attendance unique `(emp_code, date)` index both enforces one record per employee-day and resolves duplicate punch-ins. Date/employee, status/date/employee, employee/date-descending and employee/punch-in indexes support attendance listing, filters and latest-punch-out lookup. Employee department/join-date and join-date indexes support headcount and trend queries. I did not add a standalone `status` index: status is low-cardinality and the workload needs date ordering too.

2. **Punch-in race.** Both requests derive the same natural key and attempt insertion. MongoDB's unique index accepts one; the other insert raises `DuplicateKeyError`, which the API converts to 409. No check-then-insert outcome is trusted.

3. **Ties.** `$rank` gives tied totals the same competition rank. The pipeline filters on `rank <= limit` after ranking, so all employees tied at the cutoff are returned; `emp_code` breaks display-order ties.

4. **Headcount.** The department summary starts from employees joined by month end and left-looks up that month's records. Unwinding preserves employees with no matching logs, so they still contribute to headcount.

5. **100x scale.** I would load-test the actual explain plans and traffic first, then consider materialized daily summaries to avoid repeatedly joining and regrouping raw attendance.
