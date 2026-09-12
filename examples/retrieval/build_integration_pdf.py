"""Synthetic two-page fixture for section-aware live integration, not a real paper."""
from pathlib import Path
from reportlab.pdfgen import canvas
import pypdfium2 as pdfium

root = Path(__file__).resolve().parents[2]
target = root/'output/pdf/section-integration-fixture.pdf'
c = canvas.Canvas(str(target), pagesize=(595,842))
c.setTitle('Section integration fixture - synthetic data')
for number, lines in enumerate([
    ['Abstract', 'This is synthetic test data, not a scientific publication.',
     '', '1 Methods', 'The AX-247 instrument samples at 100 Hz.',
     'The experiment uses two GPUs and a fixed random seed.',
     '', '1.1 Calibration', 'The calibration interval is'],
    ['twelve seconds. The reference signal is white.',
     '', '2 Results', 'In this synthetic fixture the reported accuracy is 87 percent.',
     'No university name or graduation year is specified.',
     '', '3 Conclusion', 'These facts exist only to verify the document retrieval workflow.'],
],1):
    y=775
    c.setFont('Helvetica',12)
    for line in lines:
        c.drawString(45,y,line)
        y-=27
    c.showPage()
c.save()
with pdfium.PdfDocument(str(target)) as doc:
    for number in range(len(doc)):
        page=doc[number]
        bitmap=page.render(scale=1)
        bitmap.to_pil().save(root/f'artifacts/section-integration-page-{number+1}.png')
        bitmap.close()
        page.close()
print(target)
