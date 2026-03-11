"""Main script to generate beautiful ServiceNow incident reports from CSV files."""
import argparse
import pandas as pd
from pathlib import Path
from report_generator import ReportGenerator
# from demo_mode import load_demo_data  # Demo mode module not available
from incident_analyzer import IncidentAnalyzer


def main():
    """Main function to orchestrate report generation from CSV file."""
    parser = argparse.ArgumentParser(description='Generate beautiful ServiceNow incident reports from CSV')
    parser.add_argument('--input', '-i', type=str, help='Path to ServiceNow incident CSV file')
    parser.add_argument('--format', choices=['excel', 'csv', 'html', 'all'], default='html',
                        help='Output format for the report (default: html)')
    parser.add_argument('--demo', action='store_true', help='Use demo data to test report generation')
    parser.add_argument('--analyze', action='store_true', help='Run analysis on top issues by domain')
    parser.add_argument('--months', type=int, default=3, help='Number of months to analyze (default: 3)')
    parser.add_argument('--top', type=int, default=10, help='Number of top issues to show (default: 10)')
    
    args = parser.parse_args()
    
    try:
        # Check for analysis mode
        if args.analyze and args.input:
            print(f"🔍 ANALYZING INCIDENTS FROM: {args.input}")
            print(f"📅 Analysis Period: Last {args.months} months")
            print(f"🔝 Top Issues: {args.top}\n")
            
            csv_path = Path(args.input)
            if not csv_path.exists():
                print(f"❌ Error: File not found: {args.input}")
                return 1
            
            # Run analysis
            analyzer = IncidentAnalyzer(str(csv_path))
            report = analyzer.generate_analysis_report(months=args.months, top_n=args.top)
            print(report)
            
            # Save analysis to file
            output_dir = Path('reports')
            output_dir.mkdir(exist_ok=True)
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            analysis_file = output_dir / f'analysis_{timestamp}.txt'
            
            with open(analysis_file, 'w', encoding='utf-8') as f:
                f.write(report)
            
            # Cleanup old analysis files - keep only last 5
            analysis_files = sorted(output_dir.glob('analysis_*.txt'), key=lambda x: x.stat().st_mtime, reverse=True)
            if len(analysis_files) > 5:
                for old_file in analysis_files[5:]:
                    old_file.unlink()
                    print(f"🗑️  Removed old analysis: {old_file.name}")
            
            print(f"\n💾 Analysis saved to: {analysis_file}")
            return 0
        
        # Check for demo mode
        if args.demo:
            print("❌ Demo mode is not available")
            print("💡 Please provide a CSV file using --input flag")
            return 1
            # print("🎬 DEMO MODE - Using sample data")
            # print()
            # incidents = load_demo_data()
            # print(f"✓ Loaded {len(incidents)} demo incidents")
        elif args.input:
            # Load from CSV file
            print(f"📂 Loading incidents from: {args.input}")
            csv_path = Path(args.input)
            
            if not csv_path.exists():
                print(f"❌ Error: File not found: {args.input}")
                return 1
            
            # Read CSV file with data cleaning
            df = pd.read_csv(csv_path, low_memory=False)
            
            # Remove blank rows and clean data
            initial_count = len(df)
            
            # Drop rows where all values are NaN
            df = df.dropna(how='all')
            
            # Drop rows where key columns (Number, Short description) are NaN
            if 'Number' in df.columns:
                df = df.dropna(subset=['Number'])
            if 'Short description' in df.columns:
                df = df.dropna(subset=['Short description'])
            
            # Remove duplicates based on Number if present
            if 'Number' in df.columns:
                df = df.drop_duplicates(subset=['Number'], keep='first')
            
            final_count = len(df)
            incidents = df.to_dict('records')
            
            print(f"✓ Loaded {final_count:,} valid incidents from CSV")
            if initial_count != final_count:
                print(f"   (Removed {initial_count - final_count:,} blank/duplicate rows)")
        else:
            print("❌ Error: Please provide an input CSV file using --input or use --demo mode")
            print("\nExamples:")
            print("  python main.py --input incidents.csv")
            print("  python main.py --input incidents.csv --format all")
            print("  python main.py --demo")
            return 1
        
        if not incidents:
            print("❌ No incidents found in the input")
            return 1
        
        # Generate report
        print("\n📊 Generating report...")
        # Enable analysis if we have the right columns
        include_analysis = isinstance(incidents, list) and len(incidents) > 0 and 'Short description' in incidents[0]
        report_gen = ReportGenerator(incidents, include_analysis=include_analysis)
        
        files_created = []
        
        if args.format == 'excel' or args.format == 'all':
            file_path = report_gen.to_excel()
            files_created.append(file_path)
        
        if args.format == 'csv' or args.format == 'all':
            file_path = report_gen.to_csv()
            files_created.append(file_path)
        
        if args.format == 'html' or args.format == 'all':
            file_path = report_gen.to_html()
            files_created.append(file_path)
        
        # Cleanup old HTML report files - keep only last 5
        output_dir = Path('reports')
        html_files = sorted(output_dir.glob('ServicenowReport_WW*.html'), key=lambda x: x.stat().st_mtime, reverse=True)
        if len(html_files) > 5:
            for old_file in html_files[5:]:
                old_file.unlink()
                print(f"🗑️  Removed old report: {old_file.name}")
        
        print("\n✅ Report generation complete!")
        print(f"\n📁 Created {len(files_created)} file(s):")
        for file_path in files_created:
            print(f"   → {file_path}")
        
        # Display summary
        summary = report_gen.generate_summary()
        print(f"\n📈 Summary:")
        print(f"   Total Incidents: {summary['total_incidents']}")
        if summary.get('by_state'):
            print(f"  By State: {summary['by_state']}")
        if summary.get('by_priority'):
            print(f"  By Priority: {summary['by_priority']}")
        
    except Exception as e:
        print(f"Error: {e}")
        return 1
    
    return 0


if __name__ == '__main__':
    exit(main())
