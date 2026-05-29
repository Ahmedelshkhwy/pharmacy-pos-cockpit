# منصة الصيدليات - Web App

تطبيق ويب بديل لمشروع Google Apps Script القديم. المنصة ترفع ملف Excel الأسبوعي لكل فرع، تحلل الأصناف تلقائيا، ثم تعرض لوحة تحكم وتقارير:

- راكد وغياب عروض
- قرب انتهاء الصلاحية
- بطيء الحركة
- سريع الحركة
- سجل عمليات الرفع
- تصدير CSV لأي تقرير

## التشغيل المحلي

```powershell
cd "c:\Users\ahmed\Downloads\New folder (2)\files\pharmacy_system"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

افتح المتصفح على:

```text
http://127.0.0.1:5000
```

## تسجيل الدخول

أول تشغيل ينشئ مستخدم افتراضي:

```text
User: admin
Password: admin123
```

بعد الدخول افتح "تغيير كلمة المرور" من القائمة وغير كلمة المرور مباشرة.

لو عايز تحدد بيانات مختلفة قبل أول تشغيل:

```powershell
$env:PHARMACY_ADMIN_USERNAME="admin"
$env:PHARMACY_ADMIN_PASSWORD="StrongPassword123"
$env:PHARMACY_SECRET_KEY="replace-with-a-long-random-secret"
.\.venv\Scripts\python.exe app.py
```

## أعمدة ملف Excel المطلوبة

التطبيق يتعرف على الأسماء العربية أو الإنجليزية:

| الحقل | أمثلة مقبولة |
| --- | --- |
| اسم الصنف | اسم الصنف، Item Name، Product Name |
| كود الصنف | كود الصنف، SKU، Barcode |
| الكمية المباعة | الكمية المباعة، Sold، Qty Sold، Sales |
| الرصيد الحالي | الرصيد الحالي، Stock، Balance |
| تاريخ انتهاء الصلاحية | تاريخ انتهاء الصلاحية، Expiry، Exp Date |
| سعر الوحدة | سعر الوحدة، Price، Unit Price |

يوجد نموذج جاهز للفروع في:

```text
pharmacy_branch_upload_template.xlsx
```

ويمكن تنزيله من داخل المنصة من صفحة "رفع تقرير" عبر زر "تنزيل نموذج الفروع".

## منطق التصنيف

- راكد: الرصيد أكبر من صفر والمبيعات تساوي صفر.
- بطيء الحركة: المبيعات أقل من 5% من الرصيد.
- سريع الحركة: معدل الدوران أعلى من ضعف متوسط دوران الملف.
- تحذير صلاحية: تاريخ الانتهاء خلال 90 يوم.
- خطر صلاحية: تاريخ الانتهاء خلال 30 يوم.

## البيانات

يحفظ التطبيق البيانات محليا في:

```text
pharmacy_system/instance/pharmacy.db
```

وتحفظ الملفات المرفوعة في:

```text
pharmacy_system/instance/uploads
```

## بيانات تجريبية للعرض

لتجهيز Dashboard مليان ببيانات Demo:

```powershell
.\.venv\Scripts\python.exe seed_demo.py
```

هذا الأمر يمسح بيانات الأصناف وعمليات الرفع فقط، ثم ينشئ 5 فروع و60 صنف تجريبي. لا يغير مستخدمي تسجيل الدخول.

## ملاحظات

- المشروع القديم `Code.gs` ما زال موجودا كمرجع فقط.
- هذه النسخة لا تعتمد على Google Drive أو Google Sheets.
- الصيغ المدعومة حاليا: `.xlsx` و `.xlsm`.
