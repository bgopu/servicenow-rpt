"""Report generation utilities for ServiceNow incidents."""
import os
import pandas as pd
from datetime import datetime
from typing import List, Dict
import pytz
from bs4 import BeautifulSoup


class ReportGenerator:
    """Generate various reports from ServiceNow incident data."""
    
    def __init__(self, incidents: List[Dict], include_analysis: bool = False):
        """
        Initialize report generator with incident data.
        
        Args:
            incidents: List of incident dictionaries from ServiceNow or CSV
            include_analysis: Whether to include domain analysis in reports
        """
        self.incidents = incidents
        self.df = pd.DataFrame(incidents)
        self.include_analysis = include_analysis
        
        # Normalize column names (handle both formats from ServiceNow/CSV)
        column_mapping = {
            'number': 'Number',
            'short_description': 'Short description',
            'opened_at': 'Opened',
            'state': 'State',
            'priority': 'Priority',
            'assigned_to': 'Assigned to',
            'business_service': 'Business Service',
            'assignment_group': 'Assignment Group',
            'caller_id': 'Caller',
            'sys_tags': 'Tags'
        }
        self.df.rename(columns=column_mapping, inplace=True)
        
        # Parse dates and breach data (but don't filter by date - let JavaScript handle filtering)
        if 'Opened' in self.df.columns:
            try:
                ist = pytz.timezone('Asia/Kolkata')
                self.df['Opened_dt'] = pd.to_datetime(self.df['Opened'], format='ISO8601')
                
                # Parse breach data - incident is breached only if breach comments exist
                if 'u_breach_comments' in self.df.columns:
                    self.df['is_breached'] = self.df['u_breach_comments'].notna() & (self.df['u_breach_comments'].astype(str).str.strip() != '') & (self.df['u_breach_comments'].astype(str).str.lower() != 'nan')
                    # Parse breach time only for breached incidents
                    if 'calendar_stc' in self.df.columns:
                        self.df['breach_time'] = pd.to_numeric(self.df['calendar_stc'].astype(str).str.replace(',', ''), errors='coerce')
                    else:
                        self.df['breach_time'] = None
                else:
                    self.df['breach_time'] = None
                    self.df['is_breached'] = False
                
            except Exception as e:
                print(f"⚠️ Date parsing failed: {e}")
                pass  # If date parsing fails, keep all data
        
        # Ensure output directory exists
        output_dir = os.getenv('REPORT_OUTPUT_DIR', 'reports')
        os.makedirs(output_dir, exist_ok=True)
        self.output_dir = output_dir
        
        # Always add domain analysis for charts (if Short description exists)
        if 'Short description' in self.df.columns:
            self._add_domain_analysis()
        
        # Run full analysis if requested
        if include_analysis:
            pass  # Additional analysis could go here
    
    def _add_domain_analysis(self):
        """Add domain extraction to dataframe."""
        # Domain = functional master-data domain (Customer, Finance, IBDS, …) for ALL incidents
        # Category = IAO or Non IAO ownership flag
        self.df['Domain'] = self.df['Short description'].apply(self._extract_domain_simple)
        self.df['Category'] = self.df['Short description'].apply(self._extract_category)
    
    @staticmethod
    def _job_bare_name(desc_lower: str) -> str:
        """Extract the bare job name segment (before ^, with azmcd/azmxd/azm prefix stripped)."""
        job_part = desc_lower.split('^')[0].strip()
        for pfx in ('azmcd', 'azmxd', 'azm'):
            if job_part.startswith(pfx):
                return job_part[len(pfx):]
        return job_part

    def _extract_domain_simple(self, description: str) -> str:
        """Returns the functional master-data domain for ALL incidents (IAO or not)."""
        if pd.isna(description):
            return 'Other'

        desc_lower = str(description).lower()
        bare = self._job_bare_name(desc_lower)
        is_iao_prefix = any(bare.startswith(p) for p in ('cp', 'ip', 'if'))

        # For cp/ip/if IAO jobs: derive functional domain via sub-domain extraction
        if is_iao_prefix:
            sub = self._extract_sub_domain_simple(description)
            return sub if sub else 'Other'

        # IBDS (only for non-cp/ip/if jobs)
        has_ibds = any(x in desc_lower for x in ['ibds', 'ibdsingst', 'cpibdsingst'])
        if has_ibds:
            return 'IBDS'

        # Pattern-based detection (covers plain IAO-labelled jobs and regular non-IAO jobs)
        if 'finmdg' in desc_lower or 'mdgfin' in desc_lower or 'finfxmdg' in desc_lower or 'finlmdg' in desc_lower or 'mdgs4fin' in desc_lower:
            return 'Finance'
        elif 'cusmdg' in desc_lower or 'mdgcus' in desc_lower or 'entity' in desc_lower or 'entflt' in desc_lower or 'merge' in desc_lower:
            return 'Customer'
        elif 'supmdg' in desc_lower:
            return 'Supplier'
        elif any(x in desc_lower for x in ['mdg', 'mdgloc', 'mdgcom', 'mdgfin', 'mdgs4', 'locanl', 'calendar', 'calanal', 'ref', 'mdref']):
            return 'Reference'
        elif any(x in desc_lower for x in ['fin', 'finance', 'gfinfx', 'finfx']):
            return 'Finance'
        elif any(x in desc_lower for x in ['wrkr', 'worker']):
            return 'Worker'
        elif any(x in desc_lower for x in ['cus', 'customer', 'cusanl', 'actvt', 'actvtingst', 'rdmingst', 'rltn', 'azmcdmatchingst']):
            return 'Customer'
        elif any(x in desc_lower for x in ['sup', 'supplier', 'supanl', 'rltsup']):
            return 'Supplier'
        elif any(x in desc_lower for x in ['item', 'mditem', 'xedm']):
            return 'Item'
        elif any(x in desc_lower for x in ['rpt', 'report']) or (desc_lower.startswith('rlt') and 'rltn' not in desc_lower and 'rltsup' not in desc_lower):
            return 'Reporting'
        else:
            return 'Other'

    def _extract_category(self, description: str) -> str:
        """Returns 'IAO' if this job is IAO-owned, else 'Non IAO'."""
        if pd.isna(description):
            return 'Non IAO'
        desc_lower = str(description).lower()
        bare = self._job_bare_name(desc_lower)
        is_iao_prefix = any(bare.startswith(p) for p in ('cp', 'ip', 'if'))
        if desc_lower.startswith('iao') or ' iao ' in desc_lower or is_iao_prefix:
            return 'IAO'
        return 'Non IAO'

    def _extract_sub_domain_simple(self, description: str) -> str:
        """For IAO jobs (cp/ip/if prefix), return the specific sub-domain; otherwise empty (internal use only)."""
        if pd.isna(description):
            return ''
        desc_lower = str(description).lower()
        bare = self._job_bare_name(desc_lower)
        
        # Only applies to cp/ip/if jobs (not plain iao prefix, not standalone ibds)
        iao_prefix = next((p for p in ('cp', 'ip', 'if') if bare.startswith(p)), None)
        if not iao_prefix:
            return ''
        if 'ibds' in desc_lower: return 'IBDS'
        
        remainder = bare[len(iao_prefix):]
        # Detect specific domain from the remainder of the job name
        if 'ibds' in remainder: return 'IBDS'
        if 'finmdg' in remainder or 'mdgfin' in remainder or 'finfxmdg' in remainder or 'finlmdg' in remainder or 'mdgs4fin' in remainder: return 'Finance'
        if 'cusmdg' in remainder or 'entity' in remainder or 'entflt' in remainder or 'merge' in remainder: return 'Customer'
        if 'supmdg' in remainder: return 'Supplier'
        if any(x in remainder for x in ['mdg', 'mdgloc', 'mdgcom', 'mdgs4', 'locanl', 'calendar', 'cal', 'ref', 'mdref']): return 'Reference'
        if any(x in remainder for x in ['fin', 'finance', 'gfinfx', 'finfx']): return 'Finance'
        if any(x in remainder for x in ['wrkr', 'worker']): return 'Worker'
        if any(x in remainder for x in ['cus', 'customer', 'cusanl', 'actvt', 'rdmingst', 'rltn']): return 'Customer'
        if any(x in remainder for x in ['sup', 'supplier', 'supanl', 'rltsup']): return 'Supplier'
        if any(x in remainder for x in ['item', 'mditem', 'xedm']): return 'Item'
        if any(x in remainder for x in ['rpt', 'report']): return 'Reporting'
        return 'Other'
    
    def to_excel(self, filename: str = None) -> str:
        """
        Export incidents to Excel file.
        
        Args:
            filename: Output filename (default: incidents_YYYYMMDD_HHMMSS.xlsx)
            
        Returns:
            Path to created file
        """
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"incidents_{timestamp}.xlsx"
        
        filepath = os.path.join(self.output_dir, filename)
        
        self.df.to_excel(filepath, index=False, sheet_name='Incidents')
        return filepath
    
    def to_csv(self, filename: str = None) -> str:
        """
        Export incidents to CSV file.
        
        Args:
            filename: Output filename (default: incidents_YYYYMMDD_HHMMSS.csv)
            
        Returns:
            Path to created file
        """
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"incidents_{timestamp}.csv"
        
        filepath = os.path.join(self.output_dir, filename)
        
        self.df.to_csv(filepath, index=False)
        return filepath
    
    def generate_summary(self) -> Dict:
        """
        Generate summary statistics for incidents.
        
        Returns:
            Dictionary with summary metrics
        """
        # Calculate weekly trend data for chart
        weekly_trend_data = None
        if 'Opened' in self.df.columns:
            try:
                # Use IST timezone for all calculations
                ist = pytz.timezone('Asia/Kolkata')
                if 'Opened_dt' not in self.df.columns:
                    self.df['Opened_dt'] = pd.to_datetime(self.df['Opened'], format='ISO8601')
                # Get current time using IST
                now_ist = datetime.now(ist).replace(tzinfo=None)
                now = pd.Timestamp(now_ist)
                
                # Calculate weekly counts for last 8 weeks for chart
                # Use actual Intel WW calendar dates
                weekly_counts = []
                weekly_labels = []
                weekly_ranges = []
                
                # Get current date info
                current_datetime = now.to_pydatetime()
                current_year = current_datetime.year
                current_day_of_year = (current_datetime - datetime(current_year, 1, 1)).days + 1
                
                # Determine current WW number
                if current_day_of_year < 4:
                    current_ww = 1
                elif current_day_of_year <= 10:
                    current_ww = 2
                elif current_day_of_year <= 17:
                    current_ww = 3
                elif current_day_of_year <= 24:
                    current_ww = 4
                elif current_day_of_year <= 31:
                    current_ww = 5
                else:
                    current_ww = ((current_day_of_year - 4) // 7) + 2
                
                # Calculate 8 weeks back from current WW
                for i in range(7, -1, -1):
                    ww_to_calc = current_ww - (7 - i)
                    year_to_use = current_year
                    
                    # Handle year rollover for negative WW numbers
                    if ww_to_calc <= 0:
                        year_to_use = current_year - 1
                        # For 2025, assuming 52 weeks (standard year)
                        # WW52 ends around Dec 28-31
                        ww_to_calc = 52 + ww_to_calc
                    
                    # Determine start and end dates for this WW
                    # Intel WW Calendar: WW01 (Jan 1-3), WW02 (Jan 4-10), WW03 (Jan 11-17), etc.
                    if ww_to_calc == 1:
                        ww_start_day = 1
                        ww_end_day = 3
                    elif ww_to_calc == 2:
                        ww_start_day = 4
                        ww_end_day = 10
                    elif ww_to_calc == 3:
                        ww_start_day = 11
                        ww_end_day = 17
                    elif ww_to_calc == 4:
                        ww_start_day = 18
                        ww_end_day = 24
                    elif ww_to_calc == 5:
                        ww_start_day = 25
                        ww_end_day = 31
                    else:
                        # For weeks after WW05, calculate based on 7-day periods starting from Jan 4
                        # WW06 starts Feb 1 (day 32)
                        ww_start_day = (ww_to_calc - 2) * 7 + 4
                        ww_end_day = ww_start_day + 6
                    
                    # Create actual dates
                    try:
                        period_start = pd.Timestamp(datetime(year_to_use, 1, 1) + pd.Timedelta(days=ww_start_day - 1))
                        period_end = pd.Timestamp(datetime(year_to_use, 1, 1) + pd.Timedelta(days=ww_end_day - 1))
                        period_end_inclusive = period_end.replace(hour=23, minute=59, second=59)
                        
                        # Count incidents in this WW
                        count = len(self.df[(self.df['Opened_dt'] >= period_start) & (self.df['Opened_dt'] <= period_end_inclusive)])
                        weekly_counts.append(count)
                        
                        # Store date ranges for JavaScript filtering
                        weekly_ranges.append({
                            'start': period_start.strftime('%Y-%m-%d'),
                            'end': (period_end + pd.Timedelta(days=1)).strftime('%Y-%m-%d')  # Exclusive end for JS
                        })
                        
                        weekly_labels.append(f'WW{ww_to_calc:02d}')
                    except:
                        # Skip invalid dates
                        pass
                
                # Reverse arrays so oldest week is on left, newest on right
                weekly_labels.reverse()
                weekly_counts.reverse()
                weekly_ranges.reverse()
                
                weekly_trend_data = {
                    'labels': weekly_labels,
                    'counts': weekly_counts,
                    'ranges': weekly_ranges
                }
            except Exception as e:
                print(f"⚠️ Warning: Could not calculate weekly trend data: {e}")
                pass
        
        # Calculate recurrence rate (jobs failing multiple times)
        recurrence_data = None
        if 'Short description' in self.df.columns:
            # Extract job name from short description
            self.df['job_name'] = self.df['Short description'].str.extract(r'^([a-z0-9]+)', expand=False)
            job_counts = self.df['job_name'].value_counts()
            
            # Jobs with 2+ failures
            recurring_jobs = job_counts[job_counts >= 2]
            total_recurring_incidents = recurring_jobs.sum()
            
            recurrence_data = {
                'recurring_jobs': len(recurring_jobs),
                'total_recurring_incidents': int(total_recurring_incidents),
                'recurrence_rate': round((total_recurring_incidents / len(self.df)) * 100, 1) if len(self.df) > 0 else 0,
                'top_recurring': recurring_jobs.head(5).to_dict()
            }
        
        # Calculate breach statistics
        breach_stats = {
            'total_breached': int(self.df['is_breached'].sum()) if 'is_breached' in self.df.columns else 0,
            'breach_percentage': round((self.df['is_breached'].sum() / len(self.df) * 100), 1) if 'is_breached' in self.df.columns and len(self.df) > 0 else 0,
            'avg_breach_time_hours': round(self.df[self.df['is_breached']]['breach_time'].mean() / 3600, 1) if 'is_breached' in self.df.columns and self.df['is_breached'].any() else 0
        }
        
        summary = {
            'total_incidents': len(self.df),
            'by_state': self.df['State'].value_counts().to_dict() if 'State' in self.df.columns else {},
            'by_priority': self.df['Priority'].value_counts().to_dict() if 'Priority' in self.df.columns else {},
            'by_assignment_group': self.df['Assignment Group'].value_counts().head(10).to_dict() if 'Assignment Group' in self.df.columns else {},
            'breach_stats': breach_stats,
            'weekly_trend': weekly_trend_data,
            'recurrence': recurrence_data
        }
        return summary
    
    def to_html(self, filename: str = None, include_summary: bool = True) -> str:
        """
        Generate HTML report with optional summary.
        
        Args:
            filename: Output filename (default: ServicenowReport_WWxx_YYYYMMDD_HHMMSS.html)
            include_summary: Include summary statistics in report
            
        Returns:
            Path to created file
        """
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            # Compute Intel Work Week for filename
            import pytz as _pytz
            _ist = _pytz.timezone('Asia/Kolkata')
            _now = datetime.now(_ist)
            _yr_start = _ist.localize(datetime(_now.year, 1, 1))
            _days = (_now - _yr_start).days + 1
            _ww = 1 if _days < 4 else (2 if _days <= 10 else ((_days - 4) // 7) + 2)
            filename = f"ServicenowReport_WW{_ww:02d}_{timestamp}.html"
        
        filepath = os.path.join(self.output_dir, filename)
        
        from jinja2 import Template
        template = Template("""<!DOCTYPE html>
<html>
<head>
    <title>CDS ROR - ServiceNow Incident Report</title>
    <meta charset="UTF-8">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #4a7fc7 0%, #5db8e5 100%);
            padding: 20px;
            min-height: 100vh;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #4a7fc7 0%, #5db8e5 100%);
            color: white;
            padding: 40px;
            text-align: center;
        }
        .header h1 {
            font-size: 36px;
            margin-bottom: 10px;
            font-weight: 600;
        }
        .header p {
            font-size: 16px;
            opacity: 0.9;
        }
        .baseline-comparison {
            background: linear-gradient(135deg, #f5f7fa 0%, #e8eef5 100%);
            padding: 20px;
            margin: 25px 0;
            border-radius: 10px;
            border: 2px solid #5d8fc7;
        }
        .baseline-comparison h2 {
            color: #4a7fc7;
            text-align: center;
            margin-bottom: 15px;
            font-size: 20px;
        }
        .comparison-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-bottom: 15px;
        }
        .comparison-card {
            background: white;
            padding: 18px;
            border-radius: 10px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .comparison-card.baseline {
            border-left: 5px solid #e74c3c;
        }
        .comparison-card.current {
            border-left: 5px solid #4caf50;
        }
        .comparison-card h3 {
            margin-bottom: 12px;
            font-size: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .comparison-card.baseline h3 {
            color: #e74c3c;
        }
        .comparison-card.current h3 {
            color: #4caf50;
        }
        .comparison-stat {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0;
            border-bottom: 1px solid #eee;
        }
        .comparison-stat:last-child {
            border-bottom: none;
        }
        .comparison-stat .label {
            color: #333;
            font-size: 14px;
            font-weight: 600;
        }
        .comparison-stat .value {
            font-size: 18px;
            font-weight: bold;
            color: #333;
        }
        .comparison-stat .value.small {
            font-size: 16px;
        }
        .comparison-stat.total .value {
            font-size: 20px;
            color: #5d8fc7;
        }
        .trend-indicator {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            margin-left: 10px;
        }
        .trend-indicator.down {
            background: #c8e6c9;
            color: #2e7d32;
        }
        .trend-indicator.up {
            background: #ffcdd2;
            color: #c62828;
        }
        .content {
            padding: 40px;
        }
        .metrics {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }
        .metric-card {
            background: linear-gradient(135deg, #5d8fc7 0%, #6db8d5 100%);
            padding: 25px;
            border-radius: 10px;
            color: white;
            text-align: center;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }
        .metric-card:hover {
            transform: translateY(-5px);
        }
        .metric-value {
            font-size: 48px;
            font-weight: bold;
            margin-bottom: 10px;
        }
        .metric-label {
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
            opacity: 0.9;
        }
        .incident-counter {
            display: inline-block;
            background: linear-gradient(135deg, #7a8ee5 0%, #8a6ba8 100%);
            color: white;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(122, 142, 229, 0.3);
        }
        .incident-counter .count {
            font-size: 16px;
            font-weight: bold;
        }
        .alert-section {
            background: linear-gradient(135deg, #fff8e1 0%, #ffe9b5 100%);
            border-left: 4px solid #e8a85a;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
        }
        .alert-section h3 {
            color: #d84315;
            margin-bottom: 15px;
            font-size: 18px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .alert-section ul {
            list-style: none;
            padding: 0;
            margin: 0;
        }
        .alert-section li {
            padding: 10px;
            background: white;
            margin-bottom: 8px;
            border-radius: 5px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .alert-section li:last-child {
            margin-bottom: 0;
        }
        .alert-section .job-name {
            color: #333;
            font-weight: 500;
        }
        .alert-section .job-count {
            background: #e8a85a;
            color: white;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
        }
        .summary-section {
            background: #f8f9fa;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
        }
        .summary-section h2 {
            color: #7a8ee5;
            margin-bottom: 20px;
            font-size: 24px;
        }
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
        }
        .summary-box {
            background: white;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .summary-box h3 {
            color: #8a6ba8;
            margin-bottom: 12px;
            font-size: 15px;
        }
        .summary-box ul {
            list-style: none;
        }
        .summary-box li {
            padding: 6px 0;
            border-bottom: 1px solid #eee;
            display: flex;
            justify-content: space-between;
            font-size: 13px;
        }
        .summary-box li:last-child {
            border-bottom: none;
        }
        .summary-box li span:last-child {
            font-weight: bold;
            color: #7a8ee5;
        }
        .table-container {
            overflow-x: hidden;
            border-radius: 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        table {
            width: 100%;
            border-collapse: collapse;
            background: white;
            table-layout: fixed;
        }
        table td, table th {
            word-wrap: break-word;
            overflow-wrap: break-word;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            vertical-align: top;
        }
        /* Headers can wrap to avoid clipping */
        table th {
            white-space: normal;
        }
        /* Short description: 2-line clamp */
        table td.short-desc-col {
            white-space: normal;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        /* Opened date: allow 2-line wrap */
        table td.opened-col {
            white-space: normal;
            word-break: break-all;
        }
        /* ADF Error: wrap freely, height expands with content */
        table td.adf-col {
            white-space: normal;
            overflow: hidden;
            text-overflow: clip;
        }
        thead {
            background: linear-gradient(135deg, #7a8ee5 0%, #8a6ba8 100%);
            color: white;
        }
        th {
            padding: 10px 8px;
            text-align: left;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.5px;
        }
        td {
            padding: 8px 8px;
            border-bottom: 1px solid #eee;
            font-size: 13px;
        }
        tbody tr {
            transition: background 0.2s;
        }
        tbody tr:hover {
            background: #f8f9fa;
        }
        tbody tr[data-breached="true"] {
            background-color: #ffe6e6;
            border-left: 3px solid #e74c3c;
        }
        tbody tr[data-breached="true"]:hover {
            background-color: #ffd4d4;
        }
        tbody tr:last-child td {
            border-bottom: none;
        }
        /* Hide breach columns by default */
        .breach-column {
            display: none;
        }
        .breach-column.show {
            display: table-cell;
        }
        .footer {
            background: #f8f9fa;
            padding: 20px;
            text-align: center;
            color: #666;
            font-size: 14px;
        }
        .chart-container {
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }
        .charts-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
            gap: 30px;
            margin-bottom: 30px;
        }
        .chart-container h3 {
            color: #7a8ee5;
            margin-bottom: 20px;
            text-align: center;
        }
        #domainChart {
            max-width: 500px;
            margin: 0 auto;
        }
        .top-domains {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .top-domain-card {
            background: linear-gradient(135deg, #e89fb8 0%, #e87a8a 100%);
            padding: 20px;
            border-radius: 10px;
            color: white;
            text-align: center;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }
        .top-domain-card:nth-child(2) {
            background: linear-gradient(135deg, #6db8c5 0%, #5db8e5 100%);
        }
        .top-domain-card:nth-child(3) {
            background: linear-gradient(135deg, #6bd58a 0%, #5ed5b8 100%);
        }
        .top-domain-card .rank {
            font-size: 14px;
            opacity: 0.9;
            margin-bottom: 5px;
        }
        .top-domain-card .domain-name {
            font-size: 24px;
            font-weight: bold;
            margin-bottom: 10px;
        }
        .top-domain-card .count {
            font-size: 36px;
            font-weight: bold;
        }
        .domains-appreciation-container {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 20px;
            margin-bottom: 20px;
        }
        .domains-appreciation-container h2 {
            margin: 0;
        }
        /* Option 1: Compact Star Badge - Soft mint/teal with star icon
        .appreciation-badge {
            background: linear-gradient(135deg, #e8f5f0 0%, #d4ede8 100%);
            color: #2d6a5f;
            padding: 8px 14px;
            border-radius: 20px;
            border: 2px solid #7ec4b3;
            box-shadow: 0 2px 8px rgba(126, 196, 179, 0.2);
            max-width: 350px;
            font-size: 12px;
            line-height: 1.5;
            text-align: right;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .appreciation-badge::before {
            content: '⭐';
            font-size: 16px;
        }
        */
        
        /* Option 2: Subtle Side Banner with Star - Active */
        .appreciation-badge {
            background: linear-gradient(90deg, transparent 0%, #e3f2fd 50%, transparent 100%);
            color: #1565c0;
            padding: 6px 16px;
            border-left: 3px solid #42a5f5;
            border-radius: 0 6px 6px 0;
            max-width: 380px;
            font-size: 14px;
            line-height: 1.5;
            text-align: right;
            font-style: italic;
        }
        .appreciation-badge::before {
            content: '⭐ ';
        }
        
        .appreciation-badge .incident-number {
            font-weight: bold;
        }
        .appreciation-badge strong {
            font-weight: 600;
            text-transform: uppercase;
        }
        .adf-error-cell {
            font-size: 11px;
            color: #b91c1c;
            background: #fff1f2;
            padding: 5px 7px;
            border-radius: 4px;
            display: block;
            word-break: break-word;
            white-space: normal;
            border-left: 3px solid #fca5a5;
            width: 380px;
            min-width: 320px;
            max-width: 420px;
        }
        .adf-act-block {
            margin-bottom: 6px;
        }
        .adf-extra {
            display: none;
            margin-top: 5px;
            border-top: 1px solid #fca5a5;
            padding-top: 4px;
        }
        .adf-extra-label {
            font-size: 10px;
            font-weight: 700;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.4px;
            margin: 4px 0 2px 0;
        }
        .adf-extra pre {
            font-family: monospace;
            font-size: 9px;
            color: #374151;
            background: #f9fafb;
            border: 1px solid #e5e7eb;
            border-radius: 3px;
            padding: 3px 5px;
            margin: 0 0 4px 0;
            white-space: pre-wrap;
            word-break: break-all;
            max-height: 140px;
            overflow-y: auto;
        }
        .adf-pipeline {
            font-size: 10px;
            color: #6b7280;
            margin-bottom: 3px;
            font-style: italic;
        }
        .adf-act-name {
            font-weight: 600;
            color: #991b1b;
            margin-top: 4px;
            margin-bottom: 3px;
            line-height: 1.4;
        }
        .adf-badge {
            display: inline-block;
            font-size: 9px;
            font-weight: 700;
            background: #fee2e2;
            color: #7f1d1d;
            border: 1px solid #fca5a5;
            border-radius: 3px;
            padding: 0 4px;
            vertical-align: middle;
            margin-left: 3px;
            white-space: nowrap;
        }
        .adf-object {
            font-size: 10px;
            font-weight: 600;
            color: #1d4ed8;
            background: #eff6ff;
            border-left: 2px solid #93c5fd;
            padding: 1px 5px;
            margin-bottom: 3px;
            border-radius: 2px;
        }
        .adf-msg {
            color: #b91c1c;
            margin-bottom: 2px;
            line-height: 1.4;
            position: relative;
        }
        .adf-msg.clamped {
            max-height: 5.6em; /* 4 lines × 1.4 line-height */
            overflow: hidden;
        }
        .adf-msg-toggle {
            display: block;
            font-size: 10px;
            color: #1d4ed8;
            background: none;
            border: none;
            padding: 1px 0;
            margin-bottom: 4px;
            cursor: pointer;
            user-select: none;
        }
        .adf-msg-toggle:hover {
            text-decoration: underline;
        }
        .adf-details {
            margin-top: 3px;
        }
        .adf-details summary {
            font-size: 10px;
            color: #6b7280;
            cursor: pointer;
            user-select: none;
        }
        .adf-details summary:hover {
            color: #374151;
        }
        .adf-detail-msg {
            font-size: 10px;
            color: #374151;
            background: #f9fafb;
            border-left: 2px solid #d1d5db;
            padding: 3px 5px;
            margin-top: 3px;
            white-space: pre-wrap;
            word-break: break-word;
        }
        .adf-output, .adf-input {
            font-size: 10px;
            color: #374151;
            background: #fef3c7;
            border-left: 2px solid #f59e0b;
            padding: 2px 4px;
            margin-top: 3px;
            word-break: break-all;
        }
        .adf-input pre {
            margin: 2px 0 0 0;
            white-space: pre-wrap;
            word-break: break-all;
            font-family: monospace;
            font-size: 9px;
        }
    </style>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xlsx-js-style@1.2.0/dist/xlsx.bundle.js"></script>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 id="reportTitle">📊 CDS ROR - ServiceNow Incident Report</h1>
            <p id="reportSubtitle">Generated on {{ timestamp }}</p>
        </div>
        
        <div class="content">
            {% if baseline_2025 %}
            <div class="baseline-comparison">
                <h2>📊 Year-over-Year Comparison (2025 vs 2026)</h2>
                <div class="comparison-grid">
                    <div class="comparison-card baseline">
                        <h3>📅 2025 Full Year Baseline</h3>
                        <div class="comparison-stat total">
                            <span class="label">Total Incidents</span>
                            <span class="value">{{ baseline_2025.total }}</span>
                        </div>
                        <div class="comparison-stat">
                            <span class="label">Platform Outage (L0/L1) — est.</span>
                            <span class="value">{{ baseline_2025.platform_outage }}</span>
                        </div>
                        <div class="comparison-stat" style="background:#f0f8f0;border-radius:4px;padding:8px 4px;">
                            <span class="label" style="font-weight:700;">CDS Owned (excl. platform)</span>
                            <span class="value" style="color:#e74c3c;">{{ baseline_2025.cds_only }}</span>
                        </div>
                        <div class="comparison-stat">
                            <span class="label">Monthly Average</span>
                            <span class="value">{{ baseline_2025.monthly_avg }}</span>
                        </div>
                        <div class="comparison-stat">
                            <span class="label">Quarterly Breakdown</span>
                            <span class="value small">Q1: {{ baseline_2025.q1 }} | Q2: {{ baseline_2025.q2 }} | Q3: {{ baseline_2025.q3 }} | Q4: {{ baseline_2025.q4 }}</span>
                        </div>
                    </div>
                    <div class="comparison-card current">
                        <h3>✨ 2026 Year-to-Date ({{ ytd_2026.month_range }})</h3>
                        {% if ytd_2026 %}
                        <div class="comparison-stat total">
                            <span class="label">Total Incidents</span>
                            <span class="value">{{ ytd_2026.total }}</span>
                        </div>
                        <div class="comparison-stat">
                            <span class="label">Platform Outage (L0, L1) — IAO + Non IAO</span>
                            <span class="value">{{ ytd_2026.platform_outage }}</span>
                        </div>
                        <div class="comparison-stat">
                            <span class="label">Non IAO Incidents (Excl. Platform Outage)</span>
                            <span class="value">{{ ytd_2026.non_iao_count }}</span>
                        </div>
                        <div class="comparison-stat">
                            <span class="label">IAO Incidents (Excl. Platform Outage)</span>
                            <span class="value">{{ ytd_2026.iao_excl_platform }}</span>
                        </div>
                        <div class="comparison-stat" style="background:#f0f8f0;border-radius:4px;padding:8px 4px;">
                            <span class="label" style="font-weight:700;">Projected 2026 (CDS owned)</span>
                            {% set projection = (ytd_2026.cds_ror / ytd_2026.days_ytd * 365) | round(0) | int %}
                            <span class="value">{{ projection }}</span>
                            {# Compare CDS-owned vs CDS-owned — apples-to-apples #}
                            {% set vs_baseline = ((projection - baseline_2025.cds_only) / baseline_2025.cds_only * 100) | round(0) | int %}
                            {% if vs_baseline < 0 %}
                            <span class="trend-indicator down">{{ vs_baseline|abs }}% better ✓</span>
                            {% else %}
                            <span class="trend-indicator up">{{ vs_baseline }}% higher</span>
                            {% endif %}
                        </div>
                        <div class="comparison-stat" style="font-size:11px;color:#888;padding-top:6px;">
                            <span style="font-style:italic;">⚠️ 2025 platform estimate is scaled from backup data. 2026 platform incidents are excluded from projection (same logic applied to both years for a fair comparison).</span>
                        </div>
                        {% else %}
                        <div class="comparison-stat total">
                            <span class="label">No 2026 data available</span>
                        </div>
                        {% endif %}
                    </div>
                </div>
            </div>
            {% endif %}
            
            <!-- Time Period Filter at Top -->
            <div style="background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); padding: 20px; margin: 25px 0; border-radius: 10px; border: 2px solid #5d8fc7;">
                <div style="display: flex; align-items: center; justify-content: center; gap: 15px; flex-wrap: wrap;">
                    <label for="dateFilter" style="font-weight: 600; color: #334155; font-size: 16px;">📅 Filter Report by Time Period:</label>
                    <select id="dateFilter" onchange="filterByDateRange(this.value)" style="padding: 10px 16px; border: 2px solid #5d8fc7; border-radius: 8px; background: white; font-size: 15px; cursor: pointer; min-width: 200px; font-weight: 600;">
                        <optgroup label="── 2026 Quarters ──">
                            <option value="2026-q1" {{ 'selected' if default_quarter == '2026-q1' else '' }}>2026 Q1 (Jan-Mar)</option>
                            <option value="2026-q2" {{ 'selected' if default_quarter == '2026-q2' else '' }}>2026 Q2 (Apr-Jun)</option>
                            <option value="2026-q3" {{ 'selected' if default_quarter == '2026-q3' else '' }}>2026 Q3 (Jul-Sep)</option>
                            <option value="2026-q4" {{ 'selected' if default_quarter == '2026-q4' else '' }}>2026 Q4 (Oct-Dec)</option>
                        </optgroup>
                        <optgroup label="── 2026 Months ──">
                            <option value="2026-01">January 2026</option>
                            <option value="2026-02">February 2026</option>
                            <option value="2026-03">March 2026</option>
                            <option value="2026-04">April 2026</option>
                            <option value="2026-05">May 2026</option>
                            <option value="2026-06">June 2026</option>
                            <option value="2026-07">July 2026</option>
                            <option value="2026-08">August 2026</option>
                            <option value="2026-09">September 2026</option>
                            <option value="2026-10">October 2026</option>
                            <option value="2026-11">November 2026</option>
                            <option value="2026-12">December 2026</option>
                        </optgroup>
                        <optgroup label="── 2025 ──">
                            <option value="2025-q4">2025 Q4 (Oct-Dec)</option>
                        </optgroup>
                    </select>
                </div>
            </div>
            
            {% if top_domains %}
            <div class="domains-appreciation-container">
                <h2 id="topDomainsHeading" style="color: #7a8ee5;">🚨 Top 3 Domains (2026 Q1)</h2>
                {% if lowest_domain %}
                <div class="appreciation-badge" id="lowestDomainBadge">
                    <strong>{{ lowest_domain.name }}</strong> - Lowest (<span class="incident-number">{{ lowest_domain.count }}</span>) incidents among all domains
                </div>
                {% endif %}
            </div>
            <div class="top-domains" id="topDomainsContainer">
                {% for domain, count in top_domains.items() %}
                <div class="top-domain-card" onclick="filterByDomain('{{ domain }}')" style="cursor: pointer;" title="Click to filter incidents">
                    <div class="rank">#{{ loop.index }}</div>
                    <div class="domain-name">{{ domain }}</div>
                    <div class="count">{{ count }}</div>
                    <div style="font-size: 12px; opacity: 0.9; margin-top: 5px;">incidents</div>
                </div>
                {% endfor %}
            </div>
            <div style="text-align: center; margin: 10px 0; display: flex; justify-content: center; gap: 10px; flex-wrap: wrap; align-items: center;">
                <button onclick="filterByCategory('IAO')" id="iaoFilterBtn"
                        style="padding: 8px 22px; background: #7c3aed; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; font-weight: 600;"
                        title="Show only IAO incidents">🔵 IAO</button>
                <button onclick="filterByCategory('Non IAO')" id="nonIaoFilterBtn"
                        style="padding: 8px 22px; background: #0284c7; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; font-weight: 600;"
                        title="Show only Non IAO incidents">⚪ Non IAO</button>
                <button onclick="clearFilter()" id="clearFilterBtn" style="display: none; padding: 8px 20px; background: #5d8fc7; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 14px;">❌ Clear Filter</button>
            </div>
            {% endif %}
            
            {% if domain_labels or summary.weekly_trend %}
            <div class="charts-grid">
                {% if domain_labels %}
                <div class="chart-container">
                    <h3>📊 Domain Distribution</h3>
                    <canvas id="domainChart"></canvas>
                </div>
                {% endif %}
                
                {% if summary.weekly_trend %}
                <div class="chart-container">
                    <h3>📈 Weekly Incident Trend (Intel WW)</h3>
                    <canvas id="trendChart"></canvas>
                </div>
                {% endif %}
            </div>
            {% endif %}
            
            {% if summary %}
            <div class="summary-section">
                <h2>📈 Summary Statistics &nbsp;&nbsp;|&nbsp;&nbsp; <span onclick="clearAllFilters()" style="cursor: pointer; color: #5d8fc7;" title="Click to show all incidents">📊 Total Incidents: {{ summary.total_incidents }}</span></h2>
                <div class="summary-grid">
                    {% if summary.breach_stats and summary.breach_stats.total_breached > 0 %}
                    <div class="summary-box" onclick="filterByBreach()" style="cursor: pointer; background: linear-gradient(135deg, #fff5f5 0%, #ffebee 100%); border-left: 4px solid #e74c3c;" title="Click to show breached incidents only">
                        <h3>🚨 Breach Statistics</h3>
                        <ul>
                            <li>
                                <span>Total Breached</span><span>{{ summary.breach_stats.total_breached }}</span>
                            </li>
                            <li>
                                <span>Breach Rate</span><span>{{ summary.breach_stats.breach_percentage }}%</span>
                            </li>
                            {% if summary.breach_stats.avg_breach_time_hours > 0 %}
                            <li>
                                <span>Avg Breach Time</span><span>{{ summary.breach_stats.avg_breach_time_hours }}h</span>
                            </li>
                            {% endif %}
                        </ul>
                    </div>
                    {% endif %}
                    
                    {% if summary.recurrence and summary.recurrence.top_recurring %}
                    <div class="summary-box">
                        <h3>🔄 Top Recurring Jobs</h3>
                        <ul>
                        {% for job, count in summary.recurrence.top_recurring.items() %}
                            <li onclick="filterByJobName('{{ job }}')" style="cursor: pointer;" title="Click to filter by {{ job }}">
                                <span>{{ job }}</span><span>{{ count }}</span>
                            </li>
                        {% endfor %}
                        </ul>
                    </div>
                    {% endif %}
                    
                    {% if summary.by_state %}
                    <div class="summary-box">
                        <h3>By State</h3>
                        <ul>
                        {% for state, count in summary.by_state.items() %}
                            <li onclick="filterByState('{{ state }}')" style="cursor: pointer;" title="Click to filter by {{ state }}">
                                <span>{{ state }}</span><span>{{ count }}</span>
                            </li>
                        {% endfor %}
                        </ul>
                    </div>
                    {% endif %}
                    
                    {% if summary.by_priority %}
                    <div class="summary-box">
                        <h3>By Priority</h3>
                        <ul>
                        {% for priority, count in summary.by_priority.items() %}
                            <li onclick="filterByPriority('{{ priority }}')" style="cursor: pointer;" title="Click to filter by {{ priority }}">
                                <span>{{ priority }}</span><span>{{ count }}</span>
                            </li>
                        {% endfor %}
                        </ul>
                    </div>
                    {% endif %}
                    
                    {% if summary.by_assignment_group %}
                    <div class="summary-box">
                        <h3>By Assignment Group (Top 10)</h3>
                        <ul>
                        {% for group, count in summary.by_assignment_group.items() %}
                            <li onclick="filterByAssignmentGroup('{{ group }}')" style="cursor: pointer;" title="Click to filter by {{ group }}">
                                <span>{{ group }}</span><span>{{ count }}</span>
                            </li>
                        {% endfor %}
                        </ul>
                    </div>
                    {% endif %}

                    <div class="summary-box">
                        <h3>By Category</h3>
                        <ul id="categoryFilterList">
                            <li onclick="filterByCategory('IAO')" style="cursor:pointer;" title="Click to filter IAO incidents">
                                <span style="font-weight:700;color:#7c3aed;">IAO</span><span id="iaoCount">—</span>
                            </li>
                            <li onclick="filterByCategory('Non IAO')" style="cursor:pointer;" title="Click to filter Non IAO incidents">
                                <span style="font-weight:700;color:#0284c7;">Non IAO</span><span id="nonIaoCount">—</span>
                            </li>
                        </ul>
                    </div>
                </div>
            </div>
            {% endif %}
            
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 15px;">
                <h2 style="color: #7a8ee5; margin: 0;">📋 Incident Details</h2>
                
                <div style="display: flex; gap: 15px; align-items: center;">
                    
                    <!-- Time Period Filter -->
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <label for="dateFilter2" style="font-weight: 600; color: #334155; font-size: 14px;">📅 Time Period:</label>
                        <select id="dateFilter2" onchange="filterByDateRange(this.value)" style="padding: 8px 12px; border: 2px solid #cbd5e1; border-radius: 5px; background: white; font-size: 14px; cursor: pointer; min-width: 150px;">
                            <optgroup label="── 2026 Quarters ──">
                                <option value="2026-q1" {{ 'selected' if default_quarter == '2026-q1' else '' }}>2026 Q1 (Jan-Mar)</option>
                                <option value="2026-q2" {{ 'selected' if default_quarter == '2026-q2' else '' }}>2026 Q2 (Apr-Jun)</option>
                                <option value="2026-q3" {{ 'selected' if default_quarter == '2026-q3' else '' }}>2026 Q3 (Jul-Sep)</option>
                                <option value="2026-q4" {{ 'selected' if default_quarter == '2026-q4' else '' }}>2026 Q4 (Oct-Dec)</option>
                            </optgroup>
                            <optgroup label="── 2026 Months ──">
                                <option value="2026-01">January 2026</option>
                                <option value="2026-02">February 2026</option>
                                <option value="2026-03">March 2026</option>
                                <option value="2026-04">April 2026</option>
                                <option value="2026-05">May 2026</option>
                                <option value="2026-06">June 2026</option>
                                <option value="2026-07">July 2026</option>
                                <option value="2026-08">August 2026</option>
                                <option value="2026-09">September 2026</option>
                                <option value="2026-10">October 2026</option>
                                <option value="2026-11">November 2026</option>
                                <option value="2026-12">December 2026</option>
                            </optgroup>
                            <optgroup label="── 2025 ──">
                                <option value="2025-q4">2025 Q4 (Oct-Dec)</option>
                            </optgroup>
                        </select>
                    </div>
                    
                    <!-- Download Button -->
                    <button onclick="downloadExcel()" style="padding: 10px 20px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 8px;">
                        <span>📥</span> Download Excel
                    </button>
                    
                    <!-- ServiceNow Link Box -->
                    <div style="background-color: #fff3cd; padding: 12px 20px; border-radius: 8px; border-left: 5px solid #ffc107; border: 1px solid #ffeaa7; flex-shrink: 0;">
                        <p style="color: #1e293b; font-size: 14px; line-height: 1.4; margin: 0;">
                            🔗 <strong>View Incidents details in ServiceNow:</strong> 
                            <a href="https://intel.service-now.com/now/nav/ui/classic/params/target/incident_list.do%3Fsysparm_query%3Dactive%253Dtrue%255Eassignment_group.nameSTARTSWITHMaster%2520Data%2520Cloud%26sysparm_first_row%3D1%26sysparm_view%3D" 
                               target="_blank" 
                               style="color: #0071c5; text-decoration: underline; font-weight: 600;">click here</a>
                        </p>
                    </div>
                </div>
            </div>
            
            <div class="table-container">
                {{ table }}
            </div>
        </div>
        
        <div class="footer">
            <p>ServiceNow Incident Report Generator | {{ timestamp }}</p>
        </div>
    </div>
    
    {% if domain_labels %}
    <script>
        // Domain Distribution Pie Chart (declare globally)
        let domainChart;
        const ctx = document.getElementById('domainChart').getContext('2d');
        domainChart = new Chart(ctx, {
            type: 'pie',
            data: {
                labels: {{ domain_labels|safe }},
                datasets: [{
                    data: {{ domain_values|safe }},
                    backgroundColor: [
                        '#5d8fc7',
                        '#8a6ba8',
                        '#e89fb8',
                        '#e8a85a',
                        '#6db8c5',
                        '#6bd58a',
                        '#d59a65',
                        '#7a8ee5',
                        '#e87a8a',
                        '#5ed5b8',
                        '#b88a6b'
                    ],
                    borderWidth: 2,
                    borderColor: '#fff'
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                onClick: (event, activeElements) => {
                    if (activeElements.length > 0) {
                        const index = activeElements[0].index;
                        const domain = domainChart.data.labels[index];
                        filterByDomain(domain);
                    }
                },
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            padding: 15,
                            font: {
                                size: 14
                            }
                        },
                        onClick: (event, legendItem, legend) => {
                            const index = legendItem.index;
                            const domain = legend.chart.data.labels[index];
                            filterByDomain(domain);
                        }
                    },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                let label = context.label || '';
                                let value = context.parsed || 0;
                                let total = context.dataset.data.reduce((a, b) => a + b, 0);
                                let percentage = ((value / total) * 100).toFixed(1);
                                return label + ': ' + value + ' (' + percentage + '%)';
                            }
                        }
                    }
                }
            }
        });
    </script>
    {% endif %}
    
    {% if summary.weekly_trend %}
    <script>
        // Weekly Trend Line Chart
        const trendCtx = document.getElementById('trendChart').getContext('2d');
        const trendData = {{ summary.weekly_trend.counts|safe }};
        const trendLabels = {{ summary.weekly_trend.labels|safe }};
        const weekRanges = {{ summary.weekly_trend.ranges|tojson|safe }};  // Date ranges for filtering
        
        // Determine colors based on trend (green if decreasing, red if increasing)
        const trendColors = [];
        for (let i = 0; i < trendData.length; i++) {
            if (i === 0) {
                trendColors.push('rgba(93, 143, 199, 0.6)'); // neutral blue for first point
            } else {
                if (trendData[i] < trendData[i-1]) {
                    trendColors.push('rgba(107, 213, 138, 0.6)'); // green for decrease
                } else if (trendData[i] > trendData[i-1]) {
                    trendColors.push('rgba(232, 159, 184, 0.6)'); // red for increase
                } else {
                    trendColors.push('rgba(109, 184, 197, 0.6)'); // blue for stable
                }
            }
        }
        
        const trendChart = new Chart(trendCtx, {
            type: 'bar',
            data: {
                labels: trendLabels,
                datasets: [{
                    label: 'Incidents',
                    data: trendData,
                    backgroundColor: trendColors,
                    borderColor: trendColors.map(c => c.replace('0.6', '1')),
                    borderWidth: 2,
                    borderRadius: 5
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                onClick: (event, activeElements) => {
                    if (activeElements.length > 0) {
                        const index = activeElements[0].index;
                        const weekLabel = trendLabels[index];
                        filterByWeek(index);
                    }
                },
                plugins: {
                    legend: {
                        display: false
                    },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return 'Incidents: ' + context.parsed.y;
                            },
                            afterLabel: function(context) {
                                if (context.dataIndex > 0) {
                                    const current = context.parsed.y;
                                    const previous = trendData[context.dataIndex - 1];
                                    const change = current - previous;
                                    const pct = previous > 0 ? ((change / previous) * 100).toFixed(1) : 0;
                                    if (change > 0) {
                                        return '↑ +' + change + ' (' + pct + '%)';
                                    } else if (change < 0) {
                                        return '↓ ' + change + ' (' + pct + '%)';
                                    } else {
                                        return '→ No change';
                                    }
                                }
                                return '';
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        ticks: {
                            stepSize: 1
                        }
                    }
                }
            }
        });
    </script>
    {% endif %}
    
    <script>
        // ── Filter state ──────────────────────────────────────────────────────
        // activeFilters holds ALL secondary filters simultaneously, e.g.:
        //   { domain: 'Customer', assignment_group: 'CDS S2P' }
        // Date filter is tracked separately in currentDateValue.
        let activeFilters = {};
        let currentDateValue = '{{ default_quarter }}';
        const clearBtn = document.getElementById('clearFilterBtn');
        const breachTimeByRow = {{ breach_time_hours_list|safe }};

        // ── Core helpers ──────────────────────────────────────────────────────
        function _rowMatchesDate(row, dateValue) {
            if (!dateValue || dateValue === 'all') return true;
            const openedDate = row.getAttribute('data-opened-date');
            if (!openedDate) return false;
            if (dateValue.includes('-q')) {
                const [year, quarter] = dateValue.split('-q');
                if (openedDate.substring(0, 4) !== year) return false;
                const m = parseInt(openedDate.substring(5, 7));
                if (quarter === '1') return m >= 1  && m <= 3;
                if (quarter === '2') return m >= 4  && m <= 6;
                if (quarter === '3') return m >= 7  && m <= 9;
                if (quarter === '4') return m >= 10 && m <= 12;
                return false;
            }
            return openedDate.substring(0, 7) === dateValue;
        }

        function _rowMatchesAllSecondary(row, headers, cells) {
            // week filter acts as its own date override — already handled in applyFilters
            for (const [type, value] of Object.entries(activeFilters)) {
                if (type === 'domain') {
                    const i = headers.indexOf('Domain');
                    if (i === -1 || !cells[i] || cells[i].textContent.trim() !== value) return false;
                }
                else if (type === 'state') {
                    const i = headers.indexOf('State');
                    if (i === -1 || !cells[i] || cells[i].textContent.trim() !== value) return false;
                }
                else if (type === 'priority') {
                    if (row.getAttribute('data-priority') !== value) return false;
                }
                else if (type === 'category') {
                    const i = headers.indexOf('Category');
                    if (i === -1 || !cells[i] || cells[i].textContent.trim() !== value) return false;
                }
                else if (type === 'assignment_group') {
                    const i = headers.indexOf('Assignment Group');
                    if (i === -1 || !cells[i] || cells[i].textContent.trim() !== value) return false;
                }
                else if (type === 'breach') {
                    if (row.getAttribute('data-breached') !== 'true') return false;
                }
                else if (type === 'week') {
                    const weekRange = weekRanges[value];
                    const openedDate = row.getAttribute('data-opened-date');
                    if (!openedDate || !weekRange) return false;
                    const d = new Date(openedDate);
                    if (!(d >= new Date(weekRange.start) && d < new Date(weekRange.end))) return false;
                }
                else if (type === 'job') {
                    const i = headers.indexOf('Short description');
                    if (i === -1 || !cells[i] || !cells[i].textContent.toLowerCase().includes(value.toLowerCase())) return false;
                }
            }
            return true;
        }

        function _buildFilterLabel() {
            const parts = Object.entries(activeFilters).map(([type, value]) => {
                if (type === 'domain')             return `Domain: ${value}`;
                if (type === 'category')            return `Category: ${value}`;
                if (type === 'state')               return `State: ${value}`;
                if (type === 'priority')            return `Priority: ${value}`;
                if (type === 'assignment_group')    return `Group: ${value}`;
                if (type === 'breach')              return 'Breached';
                if (type === 'week')                return trendLabels[value] || `Week ${value}`;
                if (type === 'job')                 return `Job: ${value}`;
                return value;
            });
            return parts.join(' · ');
        }

        function filterByCategory(category) {
            activeFilters['category'] = category;
            applyFilters();
            document.getElementById('incidentTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        // Central function — every filter goes through here
        function applyFilters() {
            const table = document.getElementById('incidentTable');
            if (!table) return 0;
            const rows = table.querySelectorAll('tbody tr');
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim());
            let visibleCount = 0;
            const skipDate = ('week' in activeFilters); // week is its own date boundary
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                const matchDate = skipDate || _rowMatchesDate(row, currentDateValue);
                const matchAll  = _rowMatchesAllSecondary(row, headers, cells);
                if (matchDate && matchAll) { row.style.display = ''; visibleCount++; }
                else                       { row.style.display = 'none'; }
            });
            updateStats();
            updateChartsForFilter();
            // Update clear button
            if (clearBtn) {
                const label = _buildFilterLabel();
                if (label) {
                    clearBtn.style.display = 'inline-block';
                    clearBtn.textContent = `❌ Clear Filters (${visibleCount} incidents · ${label})`;
                } else {
                    clearBtn.style.display = 'none';
                }
            }
            return visibleCount;
        }
        // ─────────────────────────────────────────────────────────────────────
        
        // Store original stats for reset
        const originalStats = {
            state: {{ summary.by_state|tojson|safe if summary.by_state else '{}'|safe }},
            priority: {{ summary.by_priority|tojson|safe if summary.by_priority else '{}'|safe }},
            assignmentGroup: {{ summary.by_assignment_group|tojson|safe if summary.by_assignment_group else '{}'|safe }},
            recurringJobs: {{ summary.recurrence.top_recurring|tojson|safe if summary.recurrence and summary.recurrence.top_recurring else '{}'|safe }},
            total: {{ summary.total_incidents }}
        };
        
        // Function to update stats based on visible rows
        function updateStats() {
            const table = document.getElementById('incidentTable');
            if (!table) return;
            
            const rows = table.querySelectorAll('tbody tr');
            const stats = {
                state: {},
                priority: {},
                assignmentGroup: {},
                recurringJobs: {},
                category: {},
                total: 0,
                breached: 0
            };
            
            // Find column indices
            const headers = table.querySelectorAll('thead th');
            let stateColIndex = -1;
            let groupColIndex = -1;
            let descColIndex = -1;
            let categoryColIndex = -1;
            
            headers.forEach((header, index) => {
                const headerText = header.textContent.trim();
                if (headerText === 'State') stateColIndex = index;
                if (headerText === 'Assignment Group') groupColIndex = index;
                if (headerText === 'Short description') descColIndex = index;
                if (headerText === 'Category') categoryColIndex = index;
            });
            
            // Count visible rows
            let totalBreachTime = 0, breachTimeCount = 0;
            rows.forEach(row => {
                if (row.style.display !== 'none') {
                    stats.total++;
                    
                    const cells = row.querySelectorAll('td');
                    
                    // Count breached & accumulate breach time
                    if (row.getAttribute('data-breached') === 'true') {
                        stats.breached++;
                        const rowIdx = parseInt(row.getAttribute('data-row-index'));
                        if (!isNaN(rowIdx) && rowIdx < breachTimeByRow.length && breachTimeByRow[rowIdx] > 0) {
                            totalBreachTime += breachTimeByRow[rowIdx];
                            breachTimeCount++;
                        }
                    }
                    
                    // Count by state
                    if (stateColIndex !== -1 && cells[stateColIndex]) {
                        const state = cells[stateColIndex].textContent.trim();
                        stats.state[state] = (stats.state[state] || 0) + 1;
                    }
                    
                    // Count by priority
                    const priority = row.getAttribute('data-priority');
                    if (priority) {
                        stats.priority[priority] = (stats.priority[priority] || 0) + 1;
                    }
                    
                    // Count by assignment group
                    if (groupColIndex !== -1 && cells[groupColIndex]) {
                        const group = cells[groupColIndex].textContent.trim();
                        stats.assignmentGroup[group] = (stats.assignmentGroup[group] || 0) + 1;
                    }

                    // Count by category
                    if (categoryColIndex !== -1 && cells[categoryColIndex]) {
                        const cat = cells[categoryColIndex].textContent.trim();
                        if (cat) stats.category[cat] = (stats.category[cat] || 0) + 1;
                    }
                    
                    // Extract job name from short description and count recurring jobs
                    if (descColIndex !== -1 && cells[descColIndex]) {
                        const description = cells[descColIndex].textContent.trim();
                        // Extract job name (pattern: jobname^P01 or just jobname)
                        const match = description.match(/^([a-z0-9]+)/i);
                        if (match) {
                            const jobName = match[1];
                            stats.recurringJobs[jobName] = (stats.recurringJobs[jobName] || 0) + 1;
                        }
                    }
                }
            });
            
            // Compute avg breach time
            stats.avgBreachTimeHours = breachTimeCount > 0 ? (totalBreachTime / breachTimeCount).toFixed(1) : null;

            // Update the UI
            updateStatsUI(stats);
        }
        
        function updateChartsForFilter() {
            const table = document.getElementById('incidentTable');
            if (!table) return;

            const rows = table.querySelectorAll('tbody tr');
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim());
            const domainColIndex  = headers.indexOf('Domain');

            // domainCounts = all domain counts (for pie chart)
            // expandedCounts = same but excluding 'Other' (for Top 3 / Lowest)
            const domainCounts = {};
            const expandedCounts = {};

            if (domainColIndex !== -1) {
                rows.forEach(row => {
                    if (row.style.display !== 'none') {
                        const cells = row.querySelectorAll('td');
                        const domain = cells[domainColIndex] ? cells[domainColIndex].textContent.trim() : '';
                        if (!domain) return;

                        domainCounts[domain] = (domainCounts[domain] || 0) + 1;
                        if (domain !== 'Other') {
                            expandedCounts[domain] = (expandedCounts[domain] || 0) + 1;
                        }
                    }
                });

                // Update domain pie chart (raw)
                if (typeof domainChart !== 'undefined') {
                    const sortedDomains = Object.entries(domainCounts).sort((a, b) => b[1] - a[1]);
                    domainChart.data.labels = sortedDomains.map(e => e[0]);
                    domainChart.data.datasets[0].data = sortedDomains.map(e => e[1]);
                    domainChart.update();
                }

                // Update top 3 / lowest using expanded counts
                updateTopDomains(expandedCounts);
            }
        }

        function updateTopDomains(expandedCounts) {
            // expandedCounts already has Other excluded and IAO split into sub-domains
            const sortedDomains = Object.entries(expandedCounts)
                .sort((a, b) => b[1] - a[1]);

            const topDomainsContainer = document.getElementById('topDomainsContainer');
            const lowestDomainBadge = document.getElementById('lowestDomainBadge');

            if (topDomainsContainer) {
                topDomainsContainer.innerHTML = '';

                if (sortedDomains.length === 0) {
                    topDomainsContainer.innerHTML = `
                        <div style="text-align: center; padding: 30px; color: #94a3b8; font-size: 16px; width: 100%;">
                            📭 No incidents found for this time period
                        </div>`;
                    if (lowestDomainBadge) lowestDomainBadge.innerHTML = 'No data available for selected period';
                } else {
                    const top3 = sortedDomains.slice(0, 3);
                    top3.forEach(([label, count], index) => {
                        const card = document.createElement('div');
                        card.className = 'top-domain-card';
                        card.onclick = () => filterByDomain(label);
                        card.style.cursor = 'pointer';
                        card.title = 'Click to filter incidents';
                        card.innerHTML = `
                            <div class="rank">#${index + 1}</div>
                            <div class="domain-name">${label}</div>
                            <div class="count">${count}</div>
                            <div style="font-size: 12px; opacity: 0.9; margin-top: 5px;">incidents</div>
                        `;
                        topDomainsContainer.appendChild(card);
                    });

                    if (lowestDomainBadge) {
                        const lowest = sortedDomains[sortedDomains.length - 1];
                        lowestDomainBadge.innerHTML = `<strong>${lowest[0]}</strong> - Lowest (<span class="incident-number">${lowest[1]}</span>) incidents among all domains`;
                    }
                }
            }
        }
        
        function updateStatsUI(stats) {
            // Update total incidents display
            const totalSpan = document.querySelector('h2 span[onclick="clearAllFilters()"]');
            if (totalSpan) {
                totalSpan.innerHTML = `📊 Total Incidents: ${stats.total}`;
            }

            // Update Category counts (static spans updated directly)
            const iaoSpan = document.getElementById('iaoCount');
            const nonIaoSpan = document.getElementById('nonIaoCount');
            if (iaoSpan) iaoSpan.textContent = stats.category['IAO'] || 0;
            if (nonIaoSpan) nonIaoSpan.textContent = stats.category['Non IAO'] || 0;
            
            // Update Breach Statistics box
            const allBoxes = document.querySelectorAll('.summary-box');
            allBoxes.forEach(box => {
                const h3 = box.querySelector('h3');
                if (h3 && h3.textContent.includes('Breach Statistics')) {
                    const lis = box.querySelectorAll('li');
                    const breachRate = stats.total > 0 ? ((stats.breached / stats.total) * 100).toFixed(1) : '0.0';
                    lis.forEach(li => {
                        const spans = li.querySelectorAll('span');
                        if (spans.length >= 2) {
                            if (spans[0].textContent.includes('Total Breached')) {
                                spans[1].textContent = stats.breached;
                            } else if (spans[0].textContent.includes('Breach Rate')) {
                                spans[1].textContent = breachRate + '%';
                            } else if (spans[0].textContent.includes('Avg Breach Time')) {
                                spans[1].textContent = stats.avgBreachTimeHours ? stats.avgBreachTimeHours + 'h' : '—';
                            }
                        }
                    });
                }
            });
            
            // Update By State
            const summaryBoxes = document.querySelectorAll('.summary-box');
            summaryBoxes.forEach(box => {
                const h3 = box.querySelector('h3');
                const ul = box.querySelector('ul');
                if (!h3 || !ul) return;
                
                if (h3.textContent.includes('Top Recurring Jobs')) {
                    ul.innerHTML = '';
                    const topJobs = Object.entries(stats.recurringJobs)
                        .sort((a, b) => b[1] - a[1])
                        .slice(0, 5);
                    topJobs.forEach(([job, count]) => {
                        const li = document.createElement('li');
                        li.onclick = () => filterByJobName(job);
                        li.style.cursor = 'pointer';
                        li.title = `Click to filter by ${job}`;
                        li.innerHTML = `<span>${job}</span><span>${count}</span>`;
                        ul.appendChild(li);
                    });
                } else if (h3.textContent.includes('By State')) {
                    ul.innerHTML = '';
                    Object.entries(stats.state).sort((a, b) => b[1] - a[1]).forEach(([state, count]) => {
                        const li = document.createElement('li');
                        li.onclick = () => filterByState(state);
                        li.style.cursor = 'pointer';
                        li.title = `Click to filter by ${state}`;
                        li.innerHTML = `<span>${state}</span><span>${count}</span>`;
                        ul.appendChild(li);
                    });
                } else if (h3.textContent.includes('By Priority')) {
                    ul.innerHTML = '';
                    Object.entries(stats.priority).sort((a, b) => b[1] - a[1]).forEach(([priority, count]) => {
                        const li = document.createElement('li');
                        li.onclick = () => filterByPriority(priority);
                        li.style.cursor = 'pointer';
                        li.title = `Click to filter by ${priority}`;
                        li.innerHTML = `<span>${priority}</span><span>${count}</span>`;
                        ul.appendChild(li);
                    });
                } else if (h3.textContent.includes('By Assignment Group')) {
                    ul.innerHTML = '';
                    const topGroups = Object.entries(stats.assignmentGroup)
                        .sort((a, b) => b[1] - a[1])
                        .slice(0, 10);
                    topGroups.forEach(([group, count]) => {
                        const li = document.createElement('li');
                        li.onclick = () => filterByAssignmentGroup(group);
                        li.style.cursor = 'pointer';
                        li.title = `Click to filter by ${group}`;
                        li.innerHTML = `<span>${group}</span><span>${count}</span>`;
                        ul.appendChild(li);
                    });
                } else if (h3.textContent.includes('By Category')) {
                    ul.innerHTML = '';
                    [['IAO', '#7c3aed'], ['Non IAO', '#0284c7']].forEach(([cat, colour]) => {
                        const cnt = stats.category[cat] || 0;
                        const li = document.createElement('li');
                        li.onclick = () => filterByCategory(cat);
                        li.style.cursor = 'pointer';
                        li.title = `Click to filter by ${cat}`;
                        li.innerHTML = `<span style="font-weight:700;color:${colour};">${cat}</span><span>${cnt}</span>`;
                        ul.appendChild(li);
                    });
                }
            });
        }
        
        function restoreOriginalStats() {
            updateStatsUI(originalStats);
        }
        
        function filterByDomain(domain) {
            activeFilters['domain'] = domain;
            applyFilters();
            document.getElementById('incidentTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        function filterByBreach() {
            activeFilters['breach'] = 'breached';
            document.querySelectorAll('.breach-column').forEach(col => col.classList.add('show'));
            applyFilters();
            document.getElementById('incidentTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        function filterByState(state) {
            activeFilters['state'] = state;
            applyFilters();
            document.getElementById('incidentTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        function filterByPriority(priority) {
            activeFilters['priority'] = priority;
            applyFilters();
            document.getElementById('incidentTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        function filterByAssignmentGroup(group) {
            activeFilters['assignment_group'] = group;
            applyFilters();
            document.getElementById('incidentTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        function filterByWeek(weekIndex) {
            activeFilters['week'] = weekIndex;
            applyFilters();
            document.getElementById('incidentTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        function filterByJobName(jobName) {
            activeFilters['job'] = jobName;
            applyFilters();
            document.getElementById('incidentTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        function clearAllFilters() {
            clearFilter();
        }

        function filterByDateRange(dateValue) {
            if (dateValue === 'all') dateValue = '{{ default_quarter }}';

            // Update the persistent date value; clear any week filter (week is its own date)
            currentDateValue = dateValue;
            delete activeFilters['week'];

            // Build period label
            let periodLabel = '';
            if (dateValue.includes('-q')) {
                const [year, quarter] = dateValue.split('-q');
                const quarters = {'1': 'Q1 (Jan-Mar)', '2': 'Q2 (Apr-Jun)', '3': 'Q3 (Jul-Sep)', '4': 'Q4 (Oct-Dec)'};
                periodLabel = `${year} ${quarters[quarter]}`;
            } else {
                const months = {'01': 'January', '02': 'February', '03': 'March', '04': 'April', '05': 'May', '06': 'June',
                               '07': 'July', '08': 'August', '09': 'September', '10': 'October', '11': 'November', '12': 'December'};
                const [year, month] = dateValue.split('-');
                periodLabel = `${months[month]} ${year}`;
            }

            const visibleCount = applyFilters();

            // Update title and heading
            const reportTitle = document.getElementById('reportTitle');
            const topDomainsHeading = document.getElementById('topDomainsHeading');
            if (reportTitle) reportTitle.textContent = `📊 CDS ROR - ServiceNow Incident Report (${periodLabel})`;
            if (topDomainsHeading) topDomainsHeading.textContent = `🚨 Top 3 Domains (${periodLabel})`;

            // Sync both dropdowns
            const dateFilter = document.getElementById('dateFilter');
            const dateFilter2 = document.getElementById('dateFilter2');
            if (dateFilter) dateFilter.value = dateValue;
            if (dateFilter2) dateFilter2.value = dateValue;
        }

        function clearFilter() {
            // Keep the current date filter; remove all secondary filters
            activeFilters = {};
            document.querySelectorAll('.breach-column').forEach(col => col.classList.remove('show'));
            applyFilters();
        }

        function downloadExcel() {
            const table = document.getElementById('incidentTable');
            if (!table) return;

            // Visible rows only
            const rows = Array.from(table.querySelectorAll('tbody tr'))
                .filter(row => row.style.display !== 'none');

            const allHeaders = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim());
            const columnsToExport = ['Number', 'Caller', 'Short description', 'Domain', 'Category', 'State', 'Assignment Group', 'Opened', 'ADF Error'];
            const columnIndices = columnsToExport.map(col => allHeaders.indexOf(col));
            const finalHeaders = ['Job Name'].concat(columnsToExport.filter(col => allHeaders.includes(col)));
            const jobNames = {{ job_names | tojson }};

            // Build raw data array
            const data = [finalHeaders];
            rows.forEach((row) => {
                const originalIndex = parseInt(row.getAttribute('data-row-index') || '0');
                const allCells = Array.from(row.querySelectorAll('td'));
                const selectedCells = columnIndices.map((idx, i) => {
                    if (idx === -1 || !allCells[idx]) return '';
                    if (columnsToExport[i] === 'ADF Error')
                        return allCells[idx].getAttribute('data-adf-text') || allCells[idx].textContent.trim();
                    return allCells[idx].textContent.trim();
                });
                data.push([jobNames[originalIndex] || ''].concat(selectedCells));
            });

            // Convert to worksheet
            const ws = XLSX.utils.aoa_to_sheet(data);

            // ── Styles ────────────────────────────────────────────────────
            const hdrStyle = {
                font:  { bold: true, color: { rgb: 'FFFFFF' }, sz: 11 },
                fill:  { patternType: 'solid', fgColor: { rgb: '1E40AF' } },
                alignment: { vertical: 'center', wrapText: true },
                border: { bottom: { style: 'thin', color: { rgb: 'BFDBFE' } } }
            };
            const evenStyle = {
                fill:  { patternType: 'solid', fgColor: { rgb: 'DBEAFE' } },
                alignment: { vertical: 'top', wrapText: true },
                font:  { sz: 10 }
            };
            const oddStyle = {
                fill:  { patternType: 'solid', fgColor: { rgb: 'FFFFFF' } },
                alignment: { vertical: 'top', wrapText: true },
                font:  { sz: 10 }
            };

            const numCols = finalHeaders.length;
            const numRows = data.length;

            for (let r = 0; r < numRows; r++) {
                for (let c = 0; c < numCols; c++) {
                    const addr = XLSX.utils.encode_cell({ r, c });
                    if (!ws[addr]) ws[addr] = { t: 's', v: '' };
                    ws[addr].s = r === 0 ? hdrStyle : (r % 2 === 0 ? evenStyle : oddStyle);
                }
            }

            // Column widths
            ws['!cols'] = [
                {wch: 14},  // Job Name
                {wch: 14},  // Number
                {wch: 22},  // Caller
                {wch: 40},  // Short description
                {wch: 12},  // Domain
                {wch: 14},  // Category
                {wch: 12},  // State
                {wch: 28},  // Assignment Group
                {wch: 20},  // Opened
                {wch: 80},  // ADF Error
            ];

            // Freeze header
            ws['!freeze'] = { xSplit: 0, ySplit: 1, topLeftCell: 'A2', activePane: 'bottomLeft' };
            ws['!autofilter'] = { ref: ws['!ref'] };

            const wb = XLSX.utils.book_new();
            XLSX.utils.book_append_sheet(wb, ws, 'Incidents');
            const timestamp = new Date().toISOString().slice(0, 10);
            XLSX.writeFile(wb, `CDS_ROR_Incidents_${timestamp}.xlsx`);
        }
        
        // Initialize on page load - update Top 3 Domains and charts based on visible data
        window.addEventListener('DOMContentLoaded', function() {
            // Default view: current quarter — apply on load
            filterByDateRange('{{ default_quarter }}');
            updateChartsForFilter();

            // ADF error message expand/collapse — reveals full msg + input/output
            document.querySelectorAll('.adf-act-block').forEach(function(block) {
                var msgEl   = block.querySelector('.adf-msg');
                var extraEl = block.querySelector('.adf-extra');
                var hasExtra = extraEl && extraEl.innerHTML.trim() !== '';
                if (msgEl) msgEl.classList.add('clamped');
                var needsToggle = hasExtra || (msgEl && msgEl.scrollHeight > msgEl.clientHeight + 2);
                if (needsToggle) {
                    var btn = document.createElement('button');
                    btn.className = 'adf-msg-toggle';
                    btn.textContent = '\u25bc Show more';
                    btn.addEventListener('click', function() {
                        var collapsed = msgEl ? msgEl.classList.contains('clamped') : false;
                        if (collapsed) {
                            if (msgEl)   msgEl.classList.remove('clamped');
                            if (extraEl) extraEl.style.display = 'block';
                            btn.textContent = '\u25b2 Show less';
                        } else {
                            if (msgEl)   msgEl.classList.add('clamped');
                            if (extraEl) extraEl.style.display = 'none';
                            btn.textContent = '\u25bc Show more';
                        }
                    });
                    block.appendChild(btn);
                } else {
                    if (msgEl) msgEl.classList.remove('clamped');
                }
            });
        });
    </script>
</body>
</html>
        """)
        
        summary = self.generate_summary() if include_summary else None
        
        # Load 2025 baseline data for comparison
        baseline_2025 = None
        ytd_2026 = None
        try:
            baseline_file = os.path.join(self.output_dir, '2025_data.csv')
            if os.path.exists(baseline_file):
                baseline_df = pd.read_csv(baseline_file, skiprows=1)
                baseline_df = baseline_df[~baseline_df['Month'].isna()]
                baseline_df['Year'] = baseline_df['Year'].ffill()
                baseline_df['Quarter'] = baseline_df['Quarter'].ffill()
                baseline_df['Count'] = pd.to_numeric(baseline_df['Count'], errors='coerce')
                baseline_df = baseline_df[baseline_df['Count'].notna()]
                
                b_total = int(baseline_df['Count'].sum())
                baseline_2025 = {
                    'total': b_total,
                    'monthly_avg': round(baseline_df['Count'].mean(), 1),
                    'q1': int(baseline_df[baseline_df['Quarter'] == 'Q1']['Count'].sum()),
                    'q2': int(baseline_df[baseline_df['Quarter'] == 'Q2']['Count'].sum()),
                    'q3': int(baseline_df[baseline_df['Quarter'] == 'Q3']['Count'].sum()),
                    'q4': int(baseline_df[baseline_df['Quarter'] == 'Q4']['Count'].sum()),
                    'platform_outage': 0,
                    'cds_only': b_total,  # default: no segregation data
                }
                # Derive 2025 platform/CDS split from backup CSV (same logic as 2026 live data)
                backup_file = os.path.join(self.output_dir, 'Incidents_list_backup.csv')
                if os.path.exists(backup_file):
                    try:
                        bk = pd.read_csv(backup_file, low_memory=False)
                        bk = bk.dropna(how='all').drop_duplicates()
                        date_col_bk = next((c for c in bk.columns if 'opened' in c.lower()), None)
                        if date_col_bk:
                            bk['_opened'] = pd.to_datetime(bk[date_col_bk], format='ISO8601', errors='coerce')
                            bk25 = bk[bk['_opened'].dt.year == 2025]
                            ag_col_bk = next((c for c in bk25.columns if 'assignment' in c.lower() and 'group' in c.lower()), None)
                            if ag_col_bk:
                                plat25 = int(bk25[bk25[ag_col_bk].isin(['ICC L0', 'ESD S2P Technical L1'])].shape[0])
                                # Scale to match manually verified 2025_data.csv total (may differ from raw backup)
                                bk25_total = len(bk25)
                                if bk25_total > 0:
                                    scale = b_total / bk25_total
                                    plat25_scaled = round(plat25 * scale)
                                else:
                                    plat25_scaled = plat25
                                baseline_2025['platform_outage'] = plat25_scaled
                                baseline_2025['cds_only'] = b_total - plat25_scaled
                    except Exception as _bk_err:
                        pass  # keep defaults if backup unavailable
                
            # Calculate 2026 YTD stats for comparison section only
            if 'Opened_dt' in self.df.columns:
                df_2026 = self.df[self.df['Opened_dt'].dt.year == 2026].copy()
                # Count all incidents and separate by assignment group
                # Exclude ICC L0 and ESD S2P Technical L1 from CDS ROR ownership (platform issues)
                platform_outage_count = 0
                if 'Assignment Group' in df_2026.columns:
                    platform_outage_count = len(df_2026[df_2026['Assignment Group'].isin(['ICC L0', 'ESD S2P Technical L1'])])
                    cds_ror_count = len(df_2026[~df_2026['Assignment Group'].isin(['ICC L0', 'ESD S2P Technical L1'])])
                else:
                    cds_ror_count = len(df_2026)
                
                total_2026 = len(df_2026)
                if total_2026 > 0:
                    # Compute dynamic month range e.g. "Jan - Mar"
                    latest_month = df_2026['Opened_dt'].max().strftime('%b') if not df_2026.empty else 'Dec'
                    month_range = 'Jan - ' + latest_month
                    # IAO / Non-IAO counts for 2026 (both exclude platform outage)
                    iao_excl_platform = 0
                    if 'Category' in df_2026.columns and 'Assignment Group' in df_2026.columns:
                        iao_excl_platform = int(
                            df_2026[
                                df_2026['Category'].eq('IAO') &
                                ~df_2026['Assignment Group'].isin(['ICC L0', 'ESD S2P Technical L1'])
                            ].shape[0]
                        )
                    elif 'Category' in df_2026.columns:
                        iao_excl_platform = int(df_2026['Category'].eq('IAO').sum())
                    non_iao_excl_platform = cds_ror_count - iao_excl_platform
                    ytd_2026 = {
                        'total': total_2026,
                        'cds_ror': cds_ror_count,
                        'platform_outage': platform_outage_count,
                        'non_iao_count': non_iao_excl_platform,
                        'iao_excl_platform': iao_excl_platform,
                        'days_ytd': (datetime.now() - datetime(2026, 1, 1)).days,
                        'month_range': month_range,
                    }
        except Exception as e:
            print(f"⚠️ Could not load baseline/YTD data: {e}")
        
        # Get domain distribution for pie chart
        domain_data = {}
        top_domains = []
        lowest_domain = None
        if 'Domain' in self.df.columns:
            domain_counts = self.df['Domain'].value_counts()
            domain_data = domain_counts.to_dict()

            # Build expanded counts: Domain is already the functional name for all incidents.
            # Just exclude 'Other'; Domain values are already merged (IAO + Non IAO per domain).
            expanded = {}
            for domain, count in domain_counts.items():
                if domain == 'Other':
                    continue
                expanded[domain] = int(count)

            expanded_series = pd.Series(expanded).sort_values(ascending=False)

            # Top 3
            top_domains = expanded_series.head(3).to_dict()

            # Lowest
            if len(expanded_series) > 0:
                lowest_domain = {'name': expanded_series.index[-1], 'count': int(expanded_series.iloc[-1])}
        
        # Filter columns for the table - remove Assigned to, Priority, Opened_dt, job_name
        display_df = self.df.copy()

        # Merge ADF error data if available (produced by adf-autosys-rpt/correlate_incidents_adf.py)
        _adf_errors_path = os.path.join(self.output_dir, 'adf_errors.json')
        if os.path.exists(_adf_errors_path):
            try:
                import json as _json_adf
                import html as _html_mod
                with open(_adf_errors_path, encoding='utf-8') as _f:
                    _adf_data = _json_adf.load(_f)
                if _adf_data and 'Number' in display_df.columns:

                    def _make_adf_cell(num):
                        entry = _adf_data.get(str(num), {})
                        if not entry:
                            return ''
                        pipeline = (entry.get('pipeline', '') or '').strip()
                        activities = entry.get('activities', [])
                        # Fall back to root_cause when no activity messages
                        if not any((a.get('message') or '').strip() for a in activities):
                            root = (entry.get('root_cause', '') or '').strip()
                            if root:
                                activities = [{'message': root, 'input': '', 'output': ''}]
                        if not activities and not pipeline:
                            return ''
                        parts = []
                        if pipeline:
                            parts.append(f'<div class="adf-pipeline">&#x1F4CC; {_html_mod.escape(pipeline)}</div>')
                        for act in activities:
                            msg = (act.get('message') or '').strip()
                            inp = (act.get('input')   or '').strip()
                            out = (act.get('output')  or '').strip()
                            if not msg and not inp and not out:
                                continue
                            block = []
                            if msg:
                                block.append(f'<div class="adf-msg">{_html_mod.escape(msg)}</div>')
                            extra = []
                            if inp:
                                extra.append(f'<div class="adf-extra-label">Input</div><pre>{_html_mod.escape(inp)}</pre>')
                            if out:
                                extra.append(f'<div class="adf-extra-label">Output</div><pre>{_html_mod.escape(out)}</pre>')
                            if extra:
                                block.append(f'<div class="adf-extra">{"" .join(extra)}</div>')
                            if block:
                                parts.append(f'<div class="adf-act-block">{"" .join(block)}</div>')
                        return '<div class="adf-error-cell">' + ''.join(parts) + '</div>' if parts else ''
                    display_df['ADF Error'] = display_df['Number'].map(_make_adf_cell).fillna('')
                    print(f"✓ Merged ADF error data for {sum(1 for v in display_df['ADF Error'] if v)} incident(s)")
            except Exception as _e:
                print(f"⚠️ Could not load ADF error data: {_e}")

        # Store Opened_dt as ISO format for JavaScript filtering
        if 'Opened_dt' in display_df.columns:
            display_df['Opened_ISO'] = display_df['Opened_dt'].dt.strftime('%Y-%m-%d')
        
        # Store Priority as data attribute for filtering but hide the column
        if 'Priority' in display_df.columns:
            display_df['Priority_Hidden'] = display_df['Priority']
        
        # Store breach status as data attribute
        if 'is_breached' in display_df.columns:
            display_df['Breach_Hidden'] = display_df['is_breached']
        
        # Format breach time for display (convert seconds to hours)
        if 'breach_time' in display_df.columns:
            display_df['Breach Time'] = display_df['breach_time'].apply(
                lambda x: f"{int(x / 3600)}h {int((x % 3600) / 60)}m" if pd.notna(x) and x > 0 else ''
            )
        
        # Rename breach reason column for display
        if 'u_breach_reason' in display_df.columns:
            display_df['Breach Reason'] = display_df['u_breach_reason'].fillna('')
        
        # Add breach comments column for display
        if 'u_breach_comments' in display_df.columns:
            display_df['Breach Comments'] = display_df['u_breach_comments'].fillna('')
        
        # Extract job names for Excel export BEFORE removing the column
        # Sort by Opened date descending (newest first) so job_names stays in sync
        if 'Opened_dt' in display_df.columns:
            try:
                display_df = display_df.sort_values('Opened_dt', ascending=False).reset_index(drop=True)
            except Exception:
                pass
        job_names = display_df['job_name'].tolist() if 'job_name' in display_df.columns else [''] * len(display_df)

        # Build breach-time-per-row list (in hours, indexed to match data-row-index after sort)
        if 'breach_time' in display_df.columns and 'is_breached' in display_df.columns:
            breach_time_hours_list = [
                round(float(bt) / 3600, 1) if is_b and pd.notna(bt) and bt > 0 else 0
                for is_b, bt in zip(display_df['is_breached'], display_df['breach_time'])
            ]
        else:
            breach_time_hours_list = [0] * len(display_df)

        columns_to_remove = ['Assigned to', 'assigned_to', 'Priority', 'priority', 'Opened_dt', 'job_name', 'Opened_ISO', 'Priority_Hidden', 'breach_time', 'is_breached', 'Breach_Hidden', 'calendar_stc', 'u_breach_reason', 'u_breach_comments', 'sys_tags', 'Tags', 'Business Service', 'business_service']
        columns_to_keep_for_data = ['Opened_ISO', 'Priority_Hidden', 'Breach_Hidden']  # Keep these for data attributes
        
        for col in columns_to_remove:
            if col in display_df.columns and col not in columns_to_keep_for_data:
                display_df = display_df.drop(columns=[col])
        
        # Reorder columns: Domain then Category next to Short description
        if 'Domain' in display_df.columns and 'Short description' in display_df.columns:
            cols = display_df.columns.tolist()
            cols.remove('Domain')
            if 'Category' in cols:
                cols.remove('Category')
            if 'ADF Error' in cols:
                cols.remove('ADF Error')
            short_desc_idx = cols.index('Short description')
            cols.insert(short_desc_idx + 1, 'Domain')
            cols.insert(short_desc_idx + 2, 'Category')
            if 'ADF Error' in display_df.columns:
                cols.insert(short_desc_idx + 3, 'ADF Error')
            display_df = display_df[cols]
        
        html_table = display_df.to_html(index=False, escape=False, border=0, classes='incident-table', table_id='incidentTable')
        
        # Add data attributes to rows for filtering
        if 'Priority_Hidden' in display_df.columns or 'Opened_ISO' in display_df.columns:
            import re
            from bs4 import BeautifulSoup
            
            # Parse HTML table
            soup = BeautifulSoup(html_table, 'html.parser')
            table = soup.find('table')
            
            # Find column indices
            headers = table.find('thead').find_all('th')
            priority_col_index = -1
            opened_iso_index = -1
            breach_col_index = -1
            number_col_index = -1
            
            for idx, th in enumerate(headers):
                header_text = th.text.strip()
                if header_text == 'Priority_Hidden':
                    priority_col_index = idx
                elif header_text == 'Opened_ISO':
                    opened_iso_index = idx
                elif header_text == 'Breach_Hidden':
                    breach_col_index = idx
                elif header_text == 'Number':
                    number_col_index = idx

            # Track wrap columns (Short description, ADF Error)
            wrap_col_indices = [idx for idx, th in enumerate(headers)
                                if th.text.strip() in ('Short description', 'ADF Error')]
            short_desc_indices = [idx for idx, th in enumerate(headers)
                                  if th.text.strip() == 'Short description']
            adf_col_indices = [idx for idx, th in enumerate(headers)
                               if th.text.strip() == 'ADF Error']
            opened_col_indices = [idx for idx, th in enumerate(headers)
                                  if th.text.strip() == 'Opened']

            # Assign explicit widths to <th> so table-layout:fixed honours them
            _col_widths = {
                'Number':            '8%',
                'Opened':            '11%',
                'Caller':            '8%',
                'Short description': '17%',
                'Domain':            '6%',
                'Category':          '6%',
                'State':             '5.5%',
                'Assignment Group':  '9.5%',
                'ADF Error':         '29%',
                'Breach Time':       '5%',
                'Breach Reason':     '7%',
                'Breach Comments':   '7%',
            }
            for th in headers:
                col_name = th.text.strip()
                if col_name in _col_widths:
                    th['style'] = th.get('style', '') + f'width:{_col_widths[col_name]};'
            
            # Add data attributes and remove hidden columns
            # Remove headers first
            if priority_col_index != -1:
                headers[priority_col_index].decompose()
            if opened_iso_index != -1:
                # Adjust index if priority was removed before it
                adj_opened_idx = opened_iso_index if priority_col_index == -1 or opened_iso_index < priority_col_index else opened_iso_index - 1
                new_headers = table.find('thead').find_all('th')
                if adj_opened_idx < len(new_headers):
                    new_headers[adj_opened_idx].decompose()
            if breach_col_index != -1:
                # Adjust index based on what was removed
                adj_breach_idx = breach_col_index
                if priority_col_index != -1 and breach_col_index > priority_col_index:
                    adj_breach_idx -= 1
                if opened_iso_index != -1 and breach_col_index > opened_iso_index:
                    adj_breach_idx -= 1
                new_headers = table.find('thead').find_all('th')
                if adj_breach_idx < len(new_headers):
                    new_headers[adj_breach_idx].decompose()
            
            # ServiceNow base URL for incident links
            SNOW_BASE = 'https://intel.service-now.com'

            # Process body rows
            tbody = table.find('tbody')
            for i, tr in enumerate(tbody.find_all('tr')):
                cells = tr.find_all('td')

                # Make incident Number a clickable link to ServiceNow
                if number_col_index != -1 and number_col_index < len(cells):
                    inc_num = cells[number_col_index].text.strip()
                    if inc_num:
                        snow_url = f"{SNOW_BASE}/nav_to.do?uri=incident.do?sysparm_query=number={inc_num}"
                        link_tag = soup.new_tag('a', href=snow_url, target='_blank',
                                               style='color:#1a73e8;font-weight:600;text-decoration:underline;')
                        link_tag.string = inc_num
                        cells[number_col_index].clear()
                        cells[number_col_index].append(link_tag)

                # Add data-row-index to track original position for Excel export
                tr['data-row-index'] = i

                # Mark wrap columns so CSS can apply white-space: normal
                for wi in wrap_col_indices:
                    if wi < len(cells):
                        cells[wi]['class'] = cells[wi].get('class', []) + ['wrap-col']
                for wi in short_desc_indices:
                    if wi < len(cells):
                        cells[wi]['class'] = cells[wi].get('class', []) + ['short-desc-col']
                for wi in adf_col_indices:
                    if wi < len(cells):
                        cells[wi]['class'] = cells[wi].get('class', []) + ['adf-col']
                        # Store full plain-text ADF error for Excel export
                        cells[wi]['data-adf-text'] = cells[wi].get_text(separator=' | ', strip=True)
                for wi in opened_col_indices:
                    if wi < len(cells):
                        cells[wi]['class'] = cells[wi].get('class', []) + ['opened-col']
                
                # Add data-priority
                if priority_col_index != -1 and priority_col_index < len(cells):
                    priority = cells[priority_col_index].text.strip()
                    tr['data-priority'] = priority
                    cells[priority_col_index].decompose()
                
                # Add data-opened-date  
                # Recalculate cells after potential removal
                cells = tr.find_all('td')
                adj_opened_idx = opened_iso_index if priority_col_index == -1 or opened_iso_index < priority_col_index else opened_iso_index - 1
                if opened_iso_index != -1 and adj_opened_idx < len(cells):
                    opened_date = cells[adj_opened_idx].text.strip()
                    tr['data-opened-date'] = opened_date
                    cells[adj_opened_idx].decompose()
                
                # Add data-breached
                # Recalculate cells after potential removals
                cells = tr.find_all('td')
                adj_breach_idx = breach_col_index
                if priority_col_index != -1 and breach_col_index > priority_col_index:
                    adj_breach_idx -= 1
                if opened_iso_index != -1 and breach_col_index > opened_iso_index:
                    adj_breach_idx -= 1
                if breach_col_index != -1 and adj_breach_idx < len(cells):
                    breach_val = cells[adj_breach_idx].text.strip().lower()
                    tr['data-breached'] = 'true' if breach_val in ['true', '1', 'yes'] else 'false'
                    cells[adj_breach_idx].decompose()
            
            html_table = str(soup)
        
        # Add breach-column class to Breach Time, Breach Reason, and Breach Comments columns
        soup = BeautifulSoup(html_table, 'html.parser')
        table = soup.find('table')
        if table:
            headers = table.find('thead').find_all('th')
            breach_col_indices = []
            for idx, th in enumerate(headers):
                header_text = th.text.strip()
                if header_text in ['Breach Time', 'Breach Reason', 'Breach Comments']:
                    th['class'] = th.get('class', []) + ['breach-column']
                    breach_col_indices.append(idx)
            
            # Add class to all body cells in breach columns
            tbody = table.find('tbody')
            if tbody:
                for row_idx, tr in enumerate(tbody.find_all('tr')):
                    cells = tr.find_all('td')
                    
                    # Add breach details to title attribute for tooltip on breached incidents
                    if tr.get('data-breached') == 'true' and row_idx < len(display_df):
                        breach_details = []
                        row_data = display_df.iloc[row_idx]
                        
                        if 'Breach Time' in row_data and pd.notna(row_data['Breach Time']) and row_data['Breach Time'] != '':
                            breach_details.append(f"Breach Time: {row_data['Breach Time']}")
                        if 'Breach Reason' in row_data and pd.notna(row_data['Breach Reason']) and row_data['Breach Reason'] != '':
                            breach_details.append(f"Breach Reason: {row_data['Breach Reason']}")
                        if 'Breach Comments' in row_data and pd.notna(row_data['Breach Comments']) and row_data['Breach Comments'] != '':
                            breach_details.append(f"Breach Comments: {row_data['Breach Comments']}")
                        
                        if breach_details:
                            tr['title'] = ' | '.join(breach_details)
                    
                    # Add breach-column class to cells
                    for idx in breach_col_indices:
                        if idx < len(cells):
                            cells[idx]['class'] = cells[idx].get('class', []) + ['breach-column']
            
            html_table = str(soup)
        
        # Convert domain data to JSON for Chart.js
        import json
        domain_labels = list(domain_data.keys())
        domain_values = list(domain_data.values())
        
        _now = datetime.now()
        _q = (_now.month - 1) // 3 + 1
        default_quarter = f"{_now.year}-q{_q}"

        html_content = template.render(
            timestamp=datetime.now().strftime('%B %d, %Y at %I:%M %p'),
            summary=summary,
            table=html_table,
            domain_labels=json.dumps(domain_labels),
            domain_values=json.dumps(domain_values),
            top_domains=top_domains,
            lowest_domain=lowest_domain,
            baseline_2025=baseline_2025,
            ytd_2026=ytd_2026,
            job_names=job_names,
            default_quarter=default_quarter,
            breach_time_hours_list=json.dumps(breach_time_hours_list)
        )
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        return filepath
