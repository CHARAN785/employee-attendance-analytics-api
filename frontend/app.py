"""Streamlit dashboard for the Employee Attendance & Analytics API."""

import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import requests
import streamlit as st

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")
DEFAULT_API_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

st.set_page_config(
    page_title="Attendance & Analytics",
    page_icon="🕒",
    layout="wide",
)


def api_request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
) -> Any:
    base_url = st.session_state.api_base_url.rstrip("/")
    try:
        response = requests.request(
            method,
            f"{base_url}{path}",
            params=params,
            json=payload,
            timeout=15,
        )
    except requests.RequestException as exc:
        st.error(f"Could not reach the API at {base_url}: {exc}")
        return None

    try:
        result = response.json()
    except requests.JSONDecodeError:
        result = response.text
    if not response.ok:
        detail = result.get("detail", result) if isinstance(result, dict) else result
        st.error(f"API returned {response.status_code}: {detail}")
        return None
    return result


def epoch_milliseconds(day: date, clock: time) -> int:
    local_instant = datetime.combine(day, clock, tzinfo=IST)
    return int(local_instant.timestamp() * 1000)


def employee_codes() -> list[str]:
    result = api_request(
        "GET", "/employees", params={"page": 1, "page_size": 100}
    )
    if not isinstance(result, dict):
        return []
    return [item["emp_code"] for item in result.get("items", [])]


def show_records(items: Any) -> None:
    if not items:
        st.info("No records found.")
    elif isinstance(items, list):
        st.dataframe(items, use_container_width=True, hide_index=True)
    else:
        st.json(items)


st.title("Employee Attendance & Analytics")
st.caption("FastAPI + MongoDB attendance management")

with st.sidebar:
    st.header("API connection")
    if "api_base_url" not in st.session_state:
        st.session_state.api_base_url = DEFAULT_API_URL
    st.text_input("FastAPI base URL", key="api_base_url")
    if st.button("Check API health", use_container_width=True):
        health = api_request("GET", "/health")
        if health:
            st.success(f"API status: {health.get('status', 'unknown')}")
    st.divider()
    st.caption("Run the backend and MongoDB before using the forms.")

employee_tab, attendance_tab, analytics_tab, explain_tab = st.tabs(
    ["Employees", "Attendance", "Analytics", "MongoDB explain"]
)

with employee_tab:
    st.subheader("Employees")
    create_column, list_column = st.columns([1, 1.5])

    with create_column:
        st.markdown("#### Add employee")
        with st.form("create_employee_form", clear_on_submit=True):
            emp_code = st.text_input("Employee code", placeholder="EMP0007")
            name = st.text_input("Name")
            email = st.text_input("Email")
            department = st.text_input("Department")
            shift_start = st.time_input("Shift start (IST)", value=time(9, 30))
            shift_end = st.time_input("Shift end (IST)", value=time(18, 30))
            joined_on = st.date_input("Joined on", value=date.today())
            submitted = st.form_submit_button("Create employee")
        if submitted:
            payload = {
                "emp_code": emp_code.strip(),
                "name": name.strip(),
                "email": email.strip(),
                "department": department.strip(),
                "shift_start": shift_start.strftime("%H:%M"),
                "shift_end": shift_end.strftime("%H:%M"),
                "joined_on": joined_on.isoformat(),
            }
            created = api_request("POST", "/employees", payload=payload)
            if created:
                st.success(f"Created employee {created['emp_code']}.")
                st.json(created)

    with list_column:
        st.markdown("#### Browse employees")
        employee_department = st.text_input(
            "Filter by department (optional)", key="employee_department_filter"
        )
        employee_page = st.number_input(
            "Page", min_value=1, value=1, step=1, key="employee_page"
        )
        if st.button("Load employees", key="load_employees"):
            params: dict[str, Any] = {
                "page": int(employee_page),
                "page_size": 100,
            }
            if employee_department.strip():
                params["department"] = employee_department.strip()
            result = api_request("GET", "/employees", params=params)
            if result:
                st.caption(f"{result['total']} employee(s) in the filtered set")
                show_records(result["items"])

with attendance_tab:
    st.subheader("Punches, records, and corrections")
    punch_tab, browse_tab, correction_tab = st.tabs(
        ["Punch in / out", "Browse attendance", "Manual correction"]
    )

    with punch_tab:
        punch_in_column, punch_out_column = st.columns(2)
        codes = employee_codes()
        if not codes:
            st.info("Create an employee first, then refresh this page.")
        else:
            with punch_in_column:
                st.markdown("#### Punch in")
                with st.form("punch_in_form"):
                    punch_in_code = st.selectbox("Employee", codes, key="punch_in_code")
                    punch_in_status = st.selectbox(
                        "Status", PRESENCE_STATUSES, key="punch_in_status"
                    )
                    punch_in_day = st.date_input(
                        "Punch-in date (IST)", value=date.today(), key="punch_in_day"
                    )
                    punch_in_time = st.time_input(
                        "Punch-in time (IST)",
                        value=datetime.now(IST).time().replace(second=0, microsecond=0),
                        key="punch_in_time",
                    )
                    punch_in_submit = st.form_submit_button("Record punch-in")
                if punch_in_submit:
                    result = api_request(
                        "POST",
                        "/attendance/punch-in",
                        payload={
                            "emp_code": punch_in_code,
                            "status": punch_in_status,
                            "punched_at": epoch_milliseconds(
                                punch_in_day, punch_in_time
                            ),
                        },
                    )
                    if result:
                        st.success("Punch-in recorded.")
                        st.json(result)

            with punch_out_column:
                st.markdown("#### Punch out")
                with st.form("punch_out_form"):
                    punch_out_code = st.selectbox(
                        "Employee", codes, key="punch_out_code"
                    )
                    punch_out_day = st.date_input(
                        "Punch-out date (IST)", value=date.today(), key="punch_out_day"
                    )
                    punch_out_time = st.time_input(
                        "Punch-out time (IST)",
                        value=datetime.now(IST).time().replace(second=0, microsecond=0),
                        key="punch_out_time",
                    )
                    punch_out_submit = st.form_submit_button("Record punch-out")
                if punch_out_submit:
                    result = api_request(
                        "POST",
                        "/attendance/punch-out",
                        payload={
                            "emp_code": punch_out_code,
                            "punched_at": epoch_milliseconds(
                                punch_out_day, punch_out_time
                            ),
                        },
                    )
                    if result:
                        st.success("Punch-out recorded.")
                        st.json(result)

    with browse_tab:
        st.markdown("#### Filter attendance")
        filter_column, date_column, status_column = st.columns(3)
        with filter_column:
            attendance_code = st.text_input("Employee code", key="attendance_code")
        with date_column:
            date_range = st.date_input(
                "Date range (inclusive)",
                value=(),
                key="attendance_date_range",
            )
        with status_column:
            selected_status = st.selectbox(
                "Status", ["Any", *ALL_STATUSES], key="attendance_status"
            )
        page_column, button_column = st.columns([1, 4])
        with page_column:
            attendance_page = st.number_input(
                "Page", min_value=1, value=1, step=1, key="attendance_page"
            )
        with button_column:
            load_attendance = st.button("Load attendance")
        if load_attendance:
            params = {"page": int(attendance_page), "page_size": 100}
            if attendance_code.strip():
                params["emp_code"] = attendance_code.strip()
            if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
                params["date_from"] = date_range[0].isoformat()
                params["date_to"] = date_range[1].isoformat()
            if selected_status != "Any":
                params["status"] = selected_status
            result = api_request("GET", "/attendance", params=params)
            if result:
                st.caption(f"{result['total']} attendance record(s)")
                show_records(result["items"])

    with correction_tab:
        st.markdown("#### Regularize one attendance record")
        st.caption("Only supplied fields are corrected. The API recalculates derived values and appends an audit entry.")
        with st.form("regularize_form"):
            correction_code = st.text_input("Employee code", key="correction_code")
            correction_day = st.date_input(
                "Attendance date", value=date.today(), key="correction_day"
            )
            correction_status = st.selectbox(
                "New status (optional)",
                ["Keep current status", *ALL_STATUSES],
                key="correction_status",
            )
            change_punch_in = st.checkbox("Correct punch-in time")
            corrected_in_day = st.date_input(
                "Correct punch-in date (IST)",
                value=date.today(),
                key="corrected_in_day",
                disabled=not change_punch_in,
            )
            corrected_in_time = st.time_input(
                "Correct punch-in time (IST)",
                value=time(9, 30),
                key="corrected_in_time",
                disabled=not change_punch_in,
            )
            change_punch_out = st.checkbox("Correct punch-out time")
            corrected_out_day = st.date_input(
                "Correct punch-out date (IST)",
                value=date.today(),
                key="corrected_out_day",
                disabled=not change_punch_out,
            )
            corrected_out_time = st.time_input(
                "Correct punch-out time (IST)",
                value=time(18, 30),
                key="corrected_out_time",
                disabled=not change_punch_out,
            )
            reason = st.text_input("Reason (5-200 characters)")
            regularized_by = st.text_input("Regularized by")
            correction_submit = st.form_submit_button("Save correction")
        if correction_submit:
            payload: dict[str, Any] = {
                "reason": reason.strip(),
                "regularized_by": regularized_by.strip(),
            }
            if correction_status != "Keep current status":
                payload["status"] = correction_status
            if change_punch_in:
                payload["punch_in"] = epoch_milliseconds(
                    corrected_in_day, corrected_in_time
                )
            if change_punch_out:
                payload["punch_out"] = epoch_milliseconds(
                    corrected_out_day, corrected_out_time
                )
            result = api_request(
                "PATCH",
                f"/attendance/{correction_code.strip()}/{correction_day.isoformat()}",
                payload=payload,
            )
            if result:
                st.success("Attendance correction saved.")
                st.json(result)

with analytics_tab:
    st.subheader("Attendance analytics")
    monthly_tab, department_tab, leaderboard_tab, trend_tab = st.tabs(
        ["Employee monthly", "Department summary", "Late leaderboard", "Department trend"]
    )

    with monthly_tab:
        month_col, employee_col = st.columns(2)
        with month_col:
            monthly_month = st.text_input(
                "Month (YYYY-MM)", value=date.today().strftime("%Y-%m")
            )
        with employee_col:
            monthly_code = st.text_input("Employee code", key="monthly_code")
        if st.button("Get monthly summary"):
            result = api_request(
                "GET",
                f"/analytics/employees/{monthly_code.strip()}/monthly",
                params={"month": monthly_month},
            )
            if result:
                metric_columns = st.columns(4)
                metric_columns[0].metric("Working days", result["working_days"])
                metric_columns[1].metric("Present days", result["present_days"])
                metric_columns[2].metric("Late minutes", result["total_late_minutes"])
                metric_columns[3].metric("Attendance", f"{result['attendance_pct']}%")
                st.json(result)

    with department_tab:
        summary_month = st.text_input(
            "Month (YYYY-MM)", value=date.today().strftime("%Y-%m"), key="summary_month"
        )
        summary_department = st.text_input(
            "Department filter (optional)", key="summary_department"
        )
        if st.button("Get department summary"):
            params = {"month": summary_month}
            if summary_department.strip():
                params["department"] = summary_department.strip()
            result = api_request(
                "GET", "/analytics/departments/summary", params=params
            )
            if result:
                show_records(result["items"])

    with leaderboard_tab:
        leaderboard_month = st.text_input(
            "Month (YYYY-MM)",
            value=date.today().strftime("%Y-%m"),
            key="leaderboard_month",
        )
        rank_limit = st.number_input(
            "Rank cutoff", min_value=1, max_value=50, value=10, step=1
        )
        leaderboard_department = st.text_input(
            "Department filter (optional)", key="leaderboard_department"
        )
        if st.button("Get late leaderboard"):
            params = {"month": leaderboard_month, "limit": int(rank_limit)}
            if leaderboard_department.strip():
                params["department"] = leaderboard_department.strip()
            result = api_request(
                "GET", "/analytics/leaderboard/late", params=params
            )
            if result:
                show_records(result["items"])

    with trend_tab:
        trend_department = st.text_input("Department", key="trend_department")
        trend_from, trend_to = st.columns(2)
        with trend_from:
            from_day = st.date_input("From", value=date.today(), key="trend_from")
        with trend_to:
            to_day = st.date_input("To", value=date.today(), key="trend_to")
        if st.button("Get department trend"):
            result = api_request(
                "GET",
                f"/analytics/departments/{trend_department.strip()}/trend",
                params={"from": from_day.isoformat(), "to": to_day.isoformat()},
            )
            if result:
                rows = result["items"]
                show_records(rows)
                chart_rows = [
                    {
                        "date": row["date"],
                        "attendance rate": row["attendance_rate"],
                        "7-day average": row["moving_avg_7d"],
                    }
                    for row in rows
                    if row["attendance_rate"] is not None
                    or row["moving_avg_7d"] is not None
                ]
                if chart_rows:
                    st.line_chart(chart_rows, x="date")

with explain_tab:
    st.subheader("MongoDB execution plan")
    endpoint = st.selectbox(
        "Endpoint",
        [
            "attendance_list",
            "employee_monthly",
            "department_summary",
            "late_leaderboard",
            "department_trend",
        ],
    )
    explain_params: dict[str, Any] = {}
    if endpoint in ("employee_monthly", "late_leaderboard", "department_summary"):
        explain_params["month"] = st.text_input(
            "Month (YYYY-MM)", value=date.today().strftime("%Y-%m"), key="explain_month"
        )
    if endpoint == "employee_monthly":
        explain_params["emp_code"] = st.text_input(
            "Employee code", key="explain_emp_code"
        )
    if endpoint in ("department_summary", "late_leaderboard"):
        explain_department = st.text_input(
            "Department filter (optional)", key="explain_department"
        )
        if explain_department.strip():
            explain_params["department"] = explain_department.strip()
    if endpoint == "late_leaderboard":
        explain_params["limit"] = st.number_input(
            "Rank cutoff", min_value=1, max_value=50, value=10, key="explain_limit"
        )
    if endpoint == "attendance_list":
        explain_emp_code = st.text_input(
            "Employee code (optional)", key="explain_attendance_emp"
        )
        if explain_emp_code.strip():
            explain_params["emp_code"] = explain_emp_code.strip()
        explain_status = st.selectbox(
            "Status", ["Any", *ALL_STATUSES], key="explain_attendance_status"
        )
        if explain_status != "Any":
            explain_params["status"] = explain_status
        explain_params["page"] = st.number_input(
            "Page", min_value=1, value=1, key="explain_page"
        )
        explain_params["page_size"] = st.number_input(
            "Page size", min_value=1, max_value=100, value=20, key="explain_page_size"
        )
    if endpoint == "department_trend":
        explain_params["department"] = st.text_input(
            "Department", key="explain_trend_department"
        )
        explain_start, explain_end = st.columns(2)
        with explain_start:
            explain_params["from"] = st.date_input(
                "From", value=date.today(), key="explain_from"
            ).isoformat()
        with explain_end:
            explain_params["to"] = st.date_input(
                "To", value=date.today(), key="explain_to"
            ).isoformat()
    if st.button("Explain query"):
        result = api_request(
            "GET", f"/admin/explain/{endpoint}", params=explain_params
        )
        if result:
            st.json(result)
