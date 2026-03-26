"""Analysis utilities for ServiceNow incidents - extract domain insights from job names."""
import pandas as pd
import re
from datetime import datetime, timedelta
from typing import Dict, List
from collections import Counter


class IncidentAnalyzer:
    """Analyze incidents and extract domain information from job names."""
    
    # Domain mapping based on common job name patterns
    DOMAIN_PATTERNS = {
        'customer': ['cus', 'customer', 'cpcus', 'cusanl', 'actvt', 'actvtingst','rdmingst','rltn','azmcdmatchingst'],
        'supplier': ['sup', 'supplier', 'cpsup', 'supanl', 'rltsup'],
        'item': ['item', 'ipitem', 'ifitem', 'cpitem', 'mditem','xedm'],
        'finance': ['fin', 'finance', 'gfinfx', 'finfx'],
        'worker': ['wrkr', 'worker'],
        'ibds': ['ibds', 'ibdsingst', 'cpibdsingst'],
        'iao': ['iao'],  # jobs with cp/ip/if prefix are also IAO (detected in _extract_domain)
        'reference': ['mdg', 'mdgloc', 'mdgcom', 'mdgfin', 'mdgs4','locanl','calendar', 'calanal', 'ref', 'mdref','ifcal','cpcal'],
        'other': []
    }
    
    def __init__(self, csv_path: str):
        """Initialize analyzer with CSV file."""
        # Read CSV with proper handling
        self.df = pd.read_csv(csv_path, low_memory=False)
        
        # Clean data - remove blank rows
        self.df = self.df.dropna(how='all')
        
        # Remove rows without key data
        if 'Number' in self.df.columns:
            self.df = self.df.dropna(subset=['Number'])
        if 'Short description' in self.df.columns:
            self.df = self.df.dropna(subset=['Short description'])
        
        # Remove duplicates
        if 'Number' in self.df.columns:
            self.df = self.df.drop_duplicates(subset=['Number'], keep='first')
        
        self._prepare_data()
    
    def _prepare_data(self):
        """Prepare data for analysis."""
        # Convert Opened column to datetime
        if 'Opened' in self.df.columns:
            self.df['Opened'] = pd.to_datetime(self.df['Opened'], errors='coerce')
        
        # Extract domain from short description
        self.df['Domain'] = self.df['Short description'].apply(self._extract_domain)
        
        # Extract job name
        self.df['Job_Name'] = self.df['Short description'].apply(self._extract_job_name)
    
    def _extract_job_name(self, description: str) -> str:
        """Extract job name from description."""
        if pd.isna(description):
            return 'Unknown'
        
        # Get first word before ^ or space
        parts = str(description).split('^')
        if parts:
            job_name = parts[0].strip()
            return job_name if job_name else 'Unknown'
        return 'Unknown'
    
    @staticmethod
    def _job_bare_name(desc_lower: str) -> str:
        """Extract the bare job name segment (before ^, with azmcd/azmxd/azm prefix stripped)."""
        job_part = desc_lower.split('^')[0].strip()
        for pfx in ('azmcd', 'azmxd', 'azm'):
            if job_part.startswith(pfx):
                return job_part[len(pfx):]
        return job_part

    def _extract_domain(self, description: str) -> str:
        """Extract functional master-data domain (same logic as report_generator._extract_domain_simple)."""
        if pd.isna(description):
            return 'other'

        desc_lower = str(description).lower()
        bare = self._job_bare_name(desc_lower)
        is_iao_prefix = any(bare.startswith(p) for p in ('cp', 'ip', 'if'))

        # For cp/ip/if IAO jobs: derive functional domain from sub-domain logic
        if is_iao_prefix:
            sub = self._extract_sub_domain(description)
            return sub.lower() if sub else 'other'

        # IBDS (non-IAO prefix only)
        has_ibds = any(pattern in desc_lower for pattern in self.DOMAIN_PATTERNS['ibds'])
        if has_ibds:
            return 'ibds'

        # Pattern-based detection (covers plain IAO-labelled and non-IAO jobs)
        if 'finmdg' in desc_lower or 'mdgfin' in desc_lower or 'finfxmdg' in desc_lower or 'finlmdg' in desc_lower or 'mdgs4fin' in desc_lower:
            return 'finance'
        if 'cusmdg' in desc_lower or 'mdgcus' in desc_lower or 'entity' in desc_lower or 'entflt' in desc_lower or 'merge' in desc_lower:
            return 'customer'
        if 'supmdg' in desc_lower:
            return 'supplier'
        if any(pattern in desc_lower for pattern in self.DOMAIN_PATTERNS['reference']):
            return 'reference'
        if any(pattern in desc_lower for pattern in self.DOMAIN_PATTERNS['finance']):
            return 'finance'
        for domain, patterns in self.DOMAIN_PATTERNS.items():
            if domain in ['other', 'ibds', 'iao', 'reference', 'finance']:
                continue
            for pattern in patterns:
                if pattern in desc_lower:
                    return domain
        return 'other'

    def _extract_sub_domain(self, description: str) -> str:
        """For cp/ip/if IAO jobs, return the functional sub-domain label (internal use)."""
        if pd.isna(description):
            return ''
        desc_lower = str(description).lower()
        bare = self._job_bare_name(desc_lower)
        iao_prefix = next((p for p in ('cp', 'ip', 'if') if bare.startswith(p)), None)
        if not iao_prefix:
            return ''
        if 'ibds' in desc_lower:
            return 'IBDS'
        remainder = bare[len(iao_prefix):]
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

    def _extract_category(self, description: str) -> str:
        """Returns 'iao' if IAO-owned, else 'non iao'."""
        if pd.isna(description):
            return 'non iao'
        desc_lower = str(description).lower()
        bare = self._job_bare_name(desc_lower)
        is_iao_prefix = any(bare.startswith(p) for p in ('cp', 'ip', 'if'))
        if desc_lower.startswith('iao') or ' iao ' in desc_lower or is_iao_prefix:
            return 'iao'
        return 'non iao'
    
    def get_top_issues(self, months: int = 3, top_n: int = 10) -> pd.DataFrame:
        """
        Get top N issues from the last X months.
        
        Args:
            months: Number of months to look back
            top_n: Number of top issues to return
            
        Returns:
            DataFrame with top issues and their details
        """
        # Filter by date
        cutoff_date = datetime.now() - timedelta(days=months * 30)
        
        if 'Opened' in self.df.columns:
            recent_df = self.df[self.df['Opened'] >= cutoff_date].copy()
        else:
            recent_df = self.df.copy()
        
        # Group by Job Name and Domain
        grouped = recent_df.groupby(['Job_Name', 'Domain']).agg({
            'Number': 'count',
            'Priority': lambda x: x.mode()[0] if len(x) > 0 else 'Unknown',
            'State': lambda x: x.mode()[0] if len(x) > 0 else 'Unknown',
            'Short description': 'first'
        }).reset_index()
        
        grouped.columns = ['Job_Name', 'Domain', 'Count', 'Common_Priority', 'Common_State', 'Example_Description']
        
        # Sort by count
        top_issues = grouped.sort_values('Count', ascending=False).head(top_n)
        
        return top_issues
    
    def get_domain_summary(self, months: int = 3) -> Dict:
        """Get summary of issues by domain."""
        cutoff_date = datetime.now() - timedelta(days=months * 30)
        
        if 'Opened' in self.df.columns:
            recent_df = self.df[self.df['Opened'] >= cutoff_date]
        else:
            recent_df = self.df
        
        domain_counts = recent_df['Domain'].value_counts().to_dict()
        
        return domain_counts
    
    def generate_analysis_report(self, months: int = 3, top_n: int = 10) -> str:
        """Generate a text report of top issues."""
        top_issues = self.get_top_issues(months, top_n)
        domain_summary = self.get_domain_summary(months)
        
        report = f"\n{'='*80}\n"
        report += f"TOP {top_n} ISSUES - LAST {months} MONTHS ANALYSIS\n"
        report += f"{'='*80}\n\n"
        
        report += f"📊 DOMAIN DISTRIBUTION:\n"
        report += f"{'-'*80}\n"
        for domain, count in sorted(domain_summary.items(), key=lambda x: x[1], reverse=True):
            report += f"  {domain.upper():20s}: {count:4d} incidents\n"
        
        report += f"\n{'='*80}\n"
        report += f"🔝 TOP {top_n} JOB FAILURES:\n"
        report += f"{'='*80}\n\n"
        
        for idx, row in top_issues.iterrows():
            report += f"#{top_issues.index.get_loc(idx) + 1}. {row['Job_Name']}\n"
            report += f"   Domain: {row['Domain'].upper()}\n"
            report += f"   Occurrences: {row['Count']}\n"
            report += f"   Common Priority: {row['Common_Priority']}\n"
            report += f"   Common State: {row['Common_State']}\n"
            report += f"   Example: {row['Example_Description'][:80]}...\n"
            report += f"{'-'*80}\n"
        
        return report
