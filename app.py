from flask import Flask, render_template, request, redirect, url_for, session, send_file
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.io as pio
from datetime import timedelta
import io

app = Flask(__name__)
app.secret_key = "retail_secret_key"

# ---------------- GLOBAL DATA ----------------
sales_df = None
inventory_df = None
forecast_df = None
reorder_df = None

@app.route("/")
def welcome():
    return render_template("welcome.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        session["user"] = request.form["email"]
        return redirect(url_for("home"))
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        session["user"] = request.form["email"]
        return redirect(url_for("login"))
    return render_template("signup.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("welcome")) 

# ---------------- HOME ----------------
@app.route("/home")
def home():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("home.html")

@app.route("/about")
def about():
    return render_template("about.html")


# ---------------- Date format----------------
def handle_date_formats(df, col="Date"):
    df[col] = pd.to_datetime(
        df[col],
        infer_datetime_format=True,
        dayfirst=True,
        errors="coerce"
    )

    invalid_count = df[col].isna().sum()
    if invalid_count > 0:
        print(f"⚠ {invalid_count} invalid date(s) found and set to NaT")

    return df


# ---------------- UPLOAD ----------------
@app.route("/upload-combined", methods=["GET", "POST"])
def upload_combined():
    global sales_df, inventory_df

    if request.method == "POST":
        df = pd.read_csv(request.files["file"])

        # ✅ Handle all date formats safely
        df = handle_date_formats(df, col="Date")

        # ✅ Split combined data
        sales_df = df[["Date", "Product_ID", "Quantity_Sold"]]

        inventory_df = (
            df[["Product_ID", "Current_Inventory", "Lead_Time"]]
            .drop_duplicates()
        )

        process_data()
        return redirect(url_for("dashboard"))

    return render_template("upload_combined.html")

@app.route("/upload-separate", methods=["GET", "POST"])
def upload_separate():
    global sales_df, inventory_df

    if request.method == "POST":
        sales_df = pd.read_csv(request.files["sales"])
        inventory_df = pd.read_csv(request.files["inventory"])

        # ✅ Handle all date formats safely
        sales_df = handle_date_formats(sales_df, col="Date")

        process_data()
        return redirect(url_for("dashboard"))

    return render_template("upload_separate.html")


# ---------------- PROCESS ----------------
def process_data():
    if sales_df is None or inventory_df is None:
        return
    process_forecasting()

# ---------------- FORECASTING & INVENTORY ----------------
def process_forecasting():
    global forecast_df, reorder_df

    forecasts = []

    for pid, group in sales_df.groupby("Product_ID"):
        avg_demand = max(group["Quantity_Sold"].mean(), 1)
        last_date = group["Date"].max()

        recent_sales = group.sort_values("Date").tail(7)["Quantity_Sold"]
        trend = recent_sales.diff().mean()
        trend = 0 if np.isnan(trend) else trend

        for i in range(1, 8):
            daily_forecast = avg_demand + (trend * i) + np.random.randint(-2, 3)
            daily_forecast = max(1, int(round(daily_forecast)))

            forecasts.append({
                "Product_ID": pid,
                "Day": last_date + timedelta(days=i),
                "Forecast": daily_forecast
            })

    forecast_df = pd.DataFrame(forecasts)

    avg_df = (
        forecast_df.groupby("Product_ID")["Forecast"]
        .mean()
        .round()
        .astype(int)
        .reset_index(name="Avg_Demand")
    )

    merged = inventory_df.merge(avg_df, on="Product_ID", how="left").fillna(0)

    merged["Safety_Stock"] = np.ceil(0.3 * merged["Avg_Demand"]).astype(int)

    merged["Reorder_Point"] = (
        merged["Avg_Demand"] * merged["Lead_Time"] + merged["Safety_Stock"]
    ).astype(int)

    # ✅ Lead-time demand logic (FIX for Action Items = 50)
    merged["Lead_Time_Demand"] = (
        merged["Avg_Demand"] * merged["Lead_Time"]
    ).astype(int)

    merged["Status"] = np.where(
        merged["Current_Inventory"] < merged["Lead_Time_Demand"],
        "Reorder Required",
        "Stock OK"
    )

    merged["Reorder_Qty"] = np.where(
        merged["Status"] == "Reorder Required",
        merged["Lead_Time_Demand"] - merged["Current_Inventory"],
        0
    ).astype(int)

    # ✅ CRITICAL FIX (you were missing this)
    reorder_df = merged

# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if forecast_df is None or forecast_df.empty or reorder_df is None:
        return redirect(url_for("home"))

    top_products = (
        reorder_df.sort_values("Avg_Demand", ascending=False)
        .head(5)["Product_ID"]
    )

    line_fig = px.line(
        forecast_df[forecast_df["Product_ID"].isin(top_products)],
        x="Day",
        y="Forecast",
        color="Product_ID",
        title="7-Day Demand Forecast (Top 5 Products)"
    )

    top_demand = (
    reorder_df.sort_values("Avg_Demand", ascending=False)
    .head(10)
)

    bar_fig = px.bar(
    top_demand,
    x="Product_ID",
    y="Avg_Demand",
    title="Average Demand per Product (Top 10)",
    text="Avg_Demand"
)

    bar_fig.update_traces(textposition="outside")


    status_counts = reorder_df["Status"].value_counts().reset_index()
    status_counts.columns = ["Status", "Count"]

    pie_fig = px.pie(
        status_counts,
        names="Status",
        values="Count",
        hole=0.4,
        title="Inventory Status Distribution"
    )

    reorder_fig = px.bar(
        reorder_df[reorder_df["Reorder_Qty"] > 0],
        x="Product_ID",
        y="Reorder_Qty",
        title="Reorder Quantity Required"
    )

    for fig in [line_fig, bar_fig, pie_fig, reorder_fig]:
        fig.update_layout(template="plotly_dark")

    total_products = reorder_df["Product_ID"].nunique()
    alerts = (reorder_df["Status"] == "Reorder Required").sum()

    reorder_risk_pct = round((alerts / total_products) * 100, 2) if total_products > 0 else 0
    avg_daily_demand = round(reorder_df["Avg_Demand"].mean(), 2)
    total_reorder_qty = int(reorder_df["Reorder_Qty"].sum())


    return render_template(
        "dashboard.html",
        line=pio.to_html(line_fig, full_html=False),
        bar=pio.to_html(bar_fig, full_html=False),
        pie=pio.to_html(pie_fig, full_html=False),
        reorder=pio.to_html(reorder_fig, full_html=False),
        total_products=reorder_df["Product_ID"].nunique(),
        alerts=(reorder_df["Status"] == "Reorder Required").sum(),
        reorder_risk_pct=reorder_risk_pct,
        avg_daily_demand=avg_daily_demand,
        total_reorder_qty=total_reorder_qty
    )

# ---------------- INVENTORY ----------------
@app.route("/inventory")
def inventory():
    if reorder_df is None:
        return redirect(url_for("home"))

    def highlight_reorder(row):
        if row["Status"] == "Reorder Required":
            return ["background-color: #ff4d4d; color: white; font-weight: bold"] * len(row)
        else:
            return [""] * len(row)

    styled = reorder_df.style.apply(highlight_reorder, axis=1)
    table = styled.to_html(index=False)

    return render_template("inventory.html", table=table)

# ---------------- SIMULATOR ----------------
@app.route("/simulator", methods=["GET", "POST"])
def simulator():
    if reorder_df is None or reorder_df.empty:
        return redirect(url_for("home"))

    result = None
    products = sorted(reorder_df["Product_ID"].unique())

    selected_pid = None
    promo_factor = ""
    holding_cost = ""
    stockout_cost = ""

    if request.method == "POST":
        selected_pid = request.form["product_id"]
        promo_factor = request.form.get("promo_factor", "0")
        holding_cost = request.form.get("holding_cost", "0")
        stockout_cost = request.form.get("stockout_cost", "0")

        row = reorder_df[reorder_df["Product_ID"] == selected_pid].iloc[0]

        base_demand = int(row["Avg_Demand"])
        lead_time = int(row["Lead_Time"])
        current_inventory = int(row["Current_Inventory"])

        # ---- Promotion-adjusted demand ----
        adjusted_demand = int(round(base_demand * (1 + float(promo_factor))))

        # ---- Safety stock (30%) ----
        safety_stock = max(1, int(0.3 * adjusted_demand))

        # ---- Reorder point ----
        reorder_point = adjusted_demand * lead_time + safety_stock

        # ---- Reorder quantity ----
        reorder_qty = max(reorder_point - current_inventory, 0)

        # ---- Inventory after order ----
        inventory_after_order = current_inventory + reorder_qty

        # ---- Overstock / Stockout AFTER order ----
        overstock_units = max(inventory_after_order - reorder_point, 0)
        shortage_units = max(reorder_point - inventory_after_order, 0)

        overstock_cost = overstock_units * float(holding_cost)
        stockout_cost_val = shortage_units * float(stockout_cost)

        # ---- Smart recommendation ----
        if stockout_cost_val > overstock_cost:
            decision = "Increase Order (High stockout risk)"
        elif overstock_cost > stockout_cost_val:
            decision = "Limit Order (High holding cost)"
        else:
            decision = "Balanced Inventory"

        result = {
            "base_demand": base_demand,
            "adjusted_demand": adjusted_demand,
            "lead_time": lead_time,
            "current_inventory": current_inventory,
            "reorder_point": reorder_point,
            "reorder_qty": reorder_qty,
            "inventory_after_order": inventory_after_order,
            "overstock_units": overstock_units,
            "shortage_units": shortage_units,
            "overstock_cost": round(overstock_cost, 2),
            "stockout_cost": round(stockout_cost_val, 2),
            "decision": decision,
            "status": "Reorder Required" if reorder_qty > 0 else "Stock OK"
        }

    return render_template(
        "simulator.html",
        products=products,
        result=result,
        selected_pid=selected_pid,
        promo_factor=promo_factor,
        holding_cost=holding_cost,
        stockout_cost=stockout_cost
    )

# ---------------- EXPORT ----------------
@app.route("/download_reorder")
def download_reorder():
    if reorder_df is None:
        return redirect(url_for("home"))

    df = reorder_df[reorder_df["Status"] == "Reorder Required"]
    output = io.StringIO()
    df.to_csv(output, index=False)
    output.seek(0)

    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="reorder_alerts.csv"
    )

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)
