# ServiceNow Incident Report Generator

Generate beautiful, professional reports from ServiceNow incident CSV files for your team.

## ✨ Features

- **📤 CSV Import**: Simply upload your ServiceNow incident CSV export
- **🎨 Beautiful HTML Reports**: Stunning, interactive reports with modern design
- **📊 Multiple Formats**: Export to HTML, Excel, or CSV
- **📈 Smart Analytics**: Automatic summary statistics and visualizations
- **🎬 Demo Mode**: Test with sample data before using real incidents

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Export Incidents from ServiceNow

1. Log into your ServiceNow instance
2. Navigate to **Incident** → **All**
3. Apply filters (e.g., "Assigned to me", specific dates, etc.)
4. Click the **export** icon or menu
5. Select **Export** → **CSV**
6. Save the file (e.g., `my_incidents.csv`)

### 3. Generate Reports

**Create a beautiful HTML report:**
```bash
python main.py --input my_incidents.csv
```

**Generate all formats (HTML, Excel, CSV):**
```bash
python main.py --input my_incidents.csv --format all
```

**Try demo mode first:**
```bash
python main.py --demo
```

## 📋 Usage Examples

### Basic HTML Report
```bash
python main.py --input incidents.csv
```

### Generate Excel Report
```bash
python main.py --input incidents.csv --format excel
```

### Generate All Formats
```bash
python main.py --input incidents.csv --format all
```

### Demo Mode (Test with Sample Data)
```bash
python main.py --demo --format all
```

## 📊 Output

Reports are automatically saved in the `reports/` directory:

- **HTML**: `reports/report_YYYYMMDD_HHMMSS.html` - Beautiful, interactive report
- **Excel**: `reports/incidents_YYYYMMDD_HHMMSS.xlsx` - Spreadsheet format
- **CSV**: `reports/incidents_YYYYMMDD_HHMMSS.csv` - Raw data export

### HTML Report Features

The HTML report includes:
- 📊 **Summary Dashboard** with key metrics
- 📈 **Visual Statistics** breakdown by state, priority, and category  
- 📋 **Detailed Table** with all incident information
- 🎨 **Modern Design** with gradient colors and smooth interactions
- 📱 **Responsive Layout** works on all screen sizes

## 🎯 Common Workflows

### Weekly Team Report
```bash
# Export incidents from last week in ServiceNow
# Then generate HTML report for email
python main.py --input weekly_incidents.csv --format html
```

### Monthly Analysis
```bash
# Export all team incidents from last month
# Generate all formats for analysis
python main.py --input monthly_incidents.csv --format all
```

### Quick Status Update
```bash
# Export your current open incidents
# Generate quick HTML report
python main.py --input my_open_incidents.csv
```

## 📁 Project Structure

```
servicenow-rpt/
├── main.py                 # Main script - run this
├── report_generator.py     # Report generation logic
├── demo_mode.py           # Demo data for testing
├── requirements.txt       # Python dependencies
├── README.md             # This file
└── reports/              # Output directory (auto-created)
```

## 🔧 Configuration

### Custom Output Directory

Create a `.env` file to customize settings:

```ini
REPORT_OUTPUT_DIR=my_reports
```

## 💡 Tips & Tricks

### Getting Clean Data from ServiceNow

1. **Apply Filters First**: Filter to exactly what you need before exporting
2. **Select Columns**: Choose relevant columns to reduce file size
3. **Date Ranges**: Export specific time periods for focused reports
4. **Save Filters**: Save common filters in ServiceNow for quick exports

### Best Practices

- ✅ Export regularly (weekly/monthly) for trend analysis
- ✅ Keep CSV files organized by date/team
- ✅ Use HTML format for presentations and emails
- ✅ Use Excel format for further analysis
- ✅ Test with `--demo` before running on real data

## 🎬 Demo Mode

Want to see what the reports look like? Use demo mode:

```bash
python main.py --demo --format all
```

This generates sample reports without needing real ServiceNow data.

## 🐛 Troubleshooting

### "File not found" Error
- Check the file path is correct
- Use absolute path: `python main.py --input C:\Users\...\incidents.csv`
- Make sure the CSV file exists

### "No incidents found"
- Check the CSV file isn't empty
- Verify it's a valid ServiceNow export
- Try opening in Excel to check formatting

### HTML Report Doesn't Open
- Right-click the file → Open with → Browser
- Or double-click the `.html` file

## 🤝 Contributing

Have ideas for better reports? Feel free to enhance this tool!

## 📄 License

This project is open source and available for personal and team use.

---

**Made with ❤️ for better incident reporting**
