import sys
sys.path.insert(0, '.')
from send_email import build_yoy_html
html = build_yoy_html()
if html:
    with open('reports/_test_yoy.html', 'w', encoding='utf-8') as f:
        f.write('<html><body style="font-family:Arial;padding:20px;">' + html + '</body></html>')
    print('OK - wrote reports/_test_yoy.html')
else:
    print('EMPTY - YoY section returned nothing')
