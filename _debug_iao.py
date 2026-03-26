import pandas as pd

df = pd.read_csv('reports/Incidents_list.csv', low_memory=False)
df = df.dropna(how='all').drop_duplicates()
date_col = next(c for c in df.columns if 'opened' in c.lower())
df['_opened'] = pd.to_datetime(df[date_col], format='ISO8601', errors='coerce')
df26 = df[df['_opened'].dt.year == 2026]

desc_col = next(c for c in df26.columns if 'short' in c.lower() or 'description' in c.lower())
ag_col = next((c for c in df26.columns if 'assignment' in c.lower() and 'group' in c.lower()), None)

print('desc column:', desc_col)
print('ag column:', ag_col)
print('total 2026:', len(df26))

is_platform = df26[ag_col].isin(['ICC L0', 'ESD S2P Technical L1']) if ag_col else pd.Series(False, index=df26.index)
non_plat = df26[~is_platform]
print('non-platform:', len(non_plat))

desc_lower = non_plat[desc_col].str.lower().fillna('')
starts_iao   = desc_lower.str.startswith('iao')
contains_iao = desc_lower.str.contains(' iao ', na=False)
has_cp_ip_if = desc_lower.str.contains(r'\b(cp|ip|if)[0-9a-z_]', regex=True, na=False)

print('startswith "iao":', starts_iao.sum())
print('contains " iao ":', contains_iao.sum())
print('cp/ip/if pattern:', has_cp_ip_if.sum())
print('total IAO (any rule):', (starts_iao | contains_iao | has_cp_ip_if).sum())
print()
print('Sample descriptions (first 15):')
for d in non_plat[desc_col].head(15).tolist():
    print(' ', d)
