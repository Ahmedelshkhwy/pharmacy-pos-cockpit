// ═══════════════════════════════════════════════════════════════
// Weekly Pharmacy Feedback Loop — Google Apps Script
// ═══════════════════════════════════════════════════════════════
// كيفية الاستخدام:
// 1. افتح Google Sheet الرئيسي
// 2. Extensions > Apps Script > الصق هذا الكود
// 3. احفظ ثم شغّل setupTrigger() مرة واحدة فقط
// ═══════════════════════════════════════════════════════════════

// ── إعدادات يمكن تعديلها ──────────────────────────────────────
const CONFIG = {
  // اسم المجلد على Google Drive الذي سترفع فيه الفروع ملفاتها
  UPLOAD_FOLDER_NAME: "رفع تقارير الفروع",

  // حدود التصنيف
  DEAD_STOCK_MAX_SALES: 0,          // مبيعات = صفر → راكد
  SLOW_MOVING_RATIO: 0.05,          // مبيعات أقل من 5% من الرصيد → بطيء
  EXPIRY_WARNING_DAYS: 90,          // أقل من 90 يوم → تحذير
  EXPIRY_CRITICAL_DAYS: 30,         // أقل من 30 يوم → خطر

  // أسماء شيتات الـ Spreadsheet
  SHEET_RAW:       "بيانات الفروع",
  SHEET_DEAD:      "راكد وغياب عروض",
  SHEET_EXPIRY:    "قرب انتهاء الصلاحية",
  SHEET_SLOW:      "بطيء الحركة",
  SHEET_FAST:      "سريع الحركة",
  SHEET_DASHBOARD: "لوحة التحكم",
  SHEET_LOG:       "سجل الرفع",
  SHEET_HISTORY:   "السجل التاريخي",

  // إعدادات التقرير الأسبوعي بالإيميل — عدّل العناوين هنا
  EMAIL_MARKETING:   "marketing@yourcompany.com",   // إيميل التسويق
  EMAIL_PURCHASING:  "purchasing@yourcompany.com",  // إيميل المشتريات
  EMAIL_ENABLED:     true,                          // اجعلها false لإيقاف الإيميل مؤقتاً
};

// ── نقطة الدخول الرئيسية ──────────────────────────────────────
// تُشغَّل تلقائياً عند رفع ملف جديد في مجلد Drive
function onNewFileUploaded(e) {
  try {
    const file = DriveApp.getFileById(e.parameter.fileId);
    processFile(file);
  } catch(err) {
    logEntry("خطأ عام: " + err.message, "ERROR");
  }
}

// ── المعالجة اليدوية (لو أردت تشغيلها يدوياً) ─────────────────
function processAllPendingFiles() {
  const folder = getOrCreateFolder(CONFIG.UPLOAD_FOLDER_NAME);
  const files   = folder.getFilesByType(MimeType.MICROSOFT_EXCEL);
  let count = 0;
  while (files.hasNext()) {
    const file = files.next();
    // تجاهل الملفات المعالجة مسبقاً (اسمها يبدأ بـ ✓)
    if (file.getName().startsWith("✓")) continue;
    processFile(file);
    count++;
  }
  // الـ UI.alert يعمل فقط من القائمة — يُتجاهل عند التشغيل التلقائي
  try { SpreadsheetApp.getUi().alert(`تمت معالجة ${count} ملف.`); } catch(_) {}

  if (count > 0) sendWeeklyReport();
}

// ── معالجة ملف واحد ───────────────────────────────────────────
function processFile(file) {
  const fileName   = file.getName();
  const branchName = extractBranchName(fileName);
  const weekDate   = Utilities.formatDate(new Date(), "Asia/Riyadh", "yyyy-MM-dd");

  logEntry(`بدء معالجة: ${fileName} | الفرع: ${branchName}`, "INFO");

  // تحويل Excel إلى Sheets مؤقت لقراءته
  const tempSheet = importExcelAsTemp(file);
  if (!tempSheet) {
    logEntry(`فشل استيراد الملف: ${fileName}`, "ERROR");
    return;
  }

  try {
    const rows = readDataRows(tempSheet);
    if (rows.length === 0) {
      logEntry(`الملف فارغ أو لا يحتوي بيانات: ${fileName}`, "WARN");
      return;
    }

    // تحليل البيانات
    const analyzed = analyzeRows(rows, branchName, weekDate);

    // كتابة النتائج في الشيتات
    appendToRaw(analyzed.all,        branchName, weekDate);
    appendToSheet(CONFIG.SHEET_DEAD,   analyzed.dead,   branchName, weekDate);
    appendToSheet(CONFIG.SHEET_EXPIRY, analyzed.expiry, branchName, weekDate);
    appendToSheet(CONFIG.SHEET_SLOW,   analyzed.slow,   branchName, weekDate);
    appendToSheet(CONFIG.SHEET_FAST,   analyzed.fast,   branchName, weekDate);

    // تحديث لوحة التحكم
    refreshDashboard();

    // إعادة تسمية الملف كـ "معالج"
    file.setName("✓ " + fileName);

    logEntry(`اكتملت المعالجة: ${rows.length} صنف | راكد: ${analyzed.dead.length} | قرب انتهاء: ${analyzed.expiry.length}`, "SUCCESS");

  } finally {
    // حذف الشيت المؤقت
    const tempId = tempSheet.getParent().getId();
    if (tempId !== SpreadsheetApp.getActiveSpreadsheet().getId()) {
      DriveApp.getFileById(tempId).setTrashed(true);
    }
  }
}

// ── قراءة صفوف البيانات من الشيت المستورد ─────────────────────
function readDataRows(sheet) {
  const data = sheet.getDataRange().getValues();
  if (data.length < 2) return [];

  // الصف الأول = رؤوس الأعمدة — نكتشف الترتيب تلقائياً
  const headers = data[0].map(h => String(h).trim().toLowerCase());
  const idx = {
    name:    findColIndex(headers, ["اسم الصنف","item name","product name","الصنف","name"]),
    sku:     findColIndex(headers, ["كود الصنف","sku","barcode","كود","رمز"]),
    sold:    findColIndex(headers, ["الكمية المباعة","مباع","sold","qty sold","sales"]),
    stock:   findColIndex(headers, ["الرصيد الحالي","رصيد","stock","balance","qty"]),
    expiry:  findColIndex(headers, ["تاريخ انتهاء الصلاحية","انتهاء","expiry","exp date","expiry date"]),
    price:   findColIndex(headers, ["سعر الوحدة","سعر","price","unit price"]),
  };

  const rows = [];
  for (let i = 1; i < data.length; i++) {
    const r = data[i];
    const name  = idx.name  >= 0 ? String(r[idx.name]  || "").trim() : "";
    const sku   = idx.sku   >= 0 ? String(r[idx.sku]   || "").trim() : "";
    const sold  = idx.sold  >= 0 ? parseNum(r[idx.sold])  : 0;
    const stock = idx.stock >= 0 ? parseNum(r[idx.stock]) : 0;
    const price = idx.price >= 0 ? parseNum(r[idx.price]) : 0;
    const expiry = idx.expiry >= 0 ? parseDate(r[idx.expiry]) : null;

    if (!name && !sku) continue; // تخطي الصفوف الفارغة

    rows.push({ name, sku, sold, stock, price, expiry,
                stockValue: stock * price,
                turnoverRatio: stock > 0 ? sold / stock : 0 });
  }
  return rows;
}

// ── تحليل وتصنيف الأصناف ──────────────────────────────────────
function analyzeRows(rows, branch, weekDate) {
  const today     = new Date();
  const dead      = [];
  const expiry    = [];
  const slow      = [];
  const fast      = [];
  const allRows   = [];

  // حساب متوسط معدل الدوران لتحديد Fast/Slow نسبياً
  const ratios    = rows.filter(r => r.stock > 0).map(r => r.turnoverRatio);
  const avgRatio  = ratios.length ? ratios.reduce((a,b)=>a+b,0)/ratios.length : 0;

  rows.forEach(row => {
    const tags = [];

    // ── 1. راكد تماماً ──
    if (row.stock > 0 && row.sold === CONFIG.DEAD_STOCK_MAX_SALES) {
      tags.push("راكد");
      dead.push({...row, branch, weekDate, reason: "مبيعات = صفر هذا الأسبوع — يحتاج عرض ترويجي"});
    }
    // ── 2. راكد نسبياً (مبيعات ضعيفة جداً) ──
    else if (row.stock > 0 && row.turnoverRatio < CONFIG.SLOW_MOVING_RATIO && row.turnoverRatio > 0) {
      tags.push("بطيء");
      slow.push({...row, branch, weekDate});
    }
    // ── 3. Fast moving (أعلى من متوسط الدوران بمرتين) ──
    else if (row.turnoverRatio > avgRatio * 2 && row.sold > 0) {
      tags.push("سريع");
      fast.push({...row, branch, weekDate});
    }

    // ── 4. قرب انتهاء الصلاحية (مستقل عن الحركة) ──
    if (row.expiry) {
      const daysLeft = Math.floor((row.expiry - today) / (1000*60*60*24));
      if (daysLeft <= CONFIG.EXPIRY_WARNING_DAYS && daysLeft >= 0) {
        const urgency = daysLeft <= CONFIG.EXPIRY_CRITICAL_DAYS ? "🔴 خطر" : "🟡 تحذير";
        expiry.push({...row, branch, weekDate, daysLeft, urgency});
        tags.push("قرب انتهاء");
      }
    }

    allRows.push({...row, branch, weekDate, tags: tags.join(" | ")});
  });

  // ترتيب: الأعلى قيمة أولاً (لأن الراكد الغالي أولوية)
  dead.sort((a,b)   => b.stockValue - a.stockValue);
  expiry.sort((a,b) => a.daysLeft   - b.daysLeft);
  slow.sort((a,b)   => b.stockValue - a.stockValue);
  fast.sort((a,b)   => b.sold       - a.sold);

  return { all: allRows, dead, expiry, slow, fast };
}

// ── كتابة البيانات الخام ───────────────────────────────────────
function appendToRaw(rows, branch, weekDate) {
  const ss    = SpreadsheetApp.getActiveSpreadsheet();
  let   sheet = ss.getSheetByName(CONFIG.SHEET_RAW);
  if (!sheet) {
    sheet = ss.insertSheet(CONFIG.SHEET_RAW);
    const hdr = ["الأسبوع","الفرع","اسم الصنف","كود الصنف (SKU)",
                 "مبيعات الأسبوع","الرصيد الحالي","سعر الوحدة",
                 "قيمة المخزون (ر.س)","معدل الدوران","تاريخ الانتهاء","التصنيف"];
    sheet.appendRow(hdr);
    styleHeaderRow(sheet, 1, hdr.length);
  }
  const toWrite = rows.map(r => [
    weekDate, branch, r.name, r.sku,
    r.sold, r.stock, r.price,
    r.stockValue.toFixed(2),
    r.turnoverRatio.toFixed(3),
    r.expiry ? Utilities.formatDate(r.expiry,"Asia/Riyadh","yyyy-MM-dd") : "",
    r.tags
  ]);
  if (toWrite.length) sheet.getRange(sheet.getLastRow()+1, 1, toWrite.length, toWrite[0].length).setValues(toWrite);
}

// ── كتابة شيتات التصنيف ───────────────────────────────────────
function appendToSheet(sheetName, rows, branch, weekDate) {
  if (!rows.length) return;
  const ss    = SpreadsheetApp.getActiveSpreadsheet();
  let   sheet = ss.getSheetByName(sheetName);

  const headersBySheet = {
    [CONFIG.SHEET_DEAD]:   ["الأسبوع","الفرع","اسم الصنف","كود الصنف","الرصيد","سعر الوحدة","قيمة المخزون (ر.س)","السبب / الإجراء المقترح"],
    [CONFIG.SHEET_EXPIRY]: ["الأسبوع","الفرع","اسم الصنف","كود الصنف","الرصيد","تاريخ الانتهاء","أيام متبقية","الحالة","الإجراء المقترح"],
    [CONFIG.SHEET_SLOW]:   ["الأسبوع","الفرع","اسم الصنف","كود الصنف","مبيعات","الرصيد","معدل الدوران","قيمة المخزون (ر.س)"],
    [CONFIG.SHEET_FAST]:   ["الأسبوع","الفرع","اسم الصنف","كود الصنف","مبيعات الأسبوع","الرصيد","معدل الدوران"],
  };

  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
    sheet.appendRow(headersBySheet[sheetName]);
    styleHeaderRow(sheet, 1, headersBySheet[sheetName].length);
  }

  const toWrite = rows.map(r => {
    if (sheetName === CONFIG.SHEET_DEAD) return [
      weekDate, branch, r.name, r.sku,
      r.stock, r.price, r.stockValue.toFixed(2), r.reason
    ];
    if (sheetName === CONFIG.SHEET_EXPIRY) {
      const action = r.daysLeft <= 14 ? "عرض فوري أو إرجاع للمورد"
                   : r.daysLeft <= 30 ? "Bundle أو تخفيض سريع"
                   : "ضع في واجهة العرض";
      return [weekDate, branch, r.name, r.sku,
              r.stock,
              Utilities.formatDate(r.expiry,"Asia/Riyadh","yyyy-MM-dd"),
              r.daysLeft, r.urgency, action];
    }
    if (sheetName === CONFIG.SHEET_SLOW) return [
      weekDate, branch, r.name, r.sku,
      r.sold, r.stock, r.turnoverRatio.toFixed(3), r.stockValue.toFixed(2)
    ];
    if (sheetName === CONFIG.SHEET_FAST) return [
      weekDate, branch, r.name, r.sku,
      r.sold, r.stock, r.turnoverRatio.toFixed(3)
    ];
  });

  sheet.getRange(sheet.getLastRow()+1, 1, toWrite.length, toWrite[0].length).setValues(toWrite);
}

// ── تحديث لوحة التحكم ─────────────────────────────────────────
function refreshDashboard() {
  const ss    = SpreadsheetApp.getActiveSpreadsheet();
  let   dash  = ss.getSheetByName(CONFIG.SHEET_DASHBOARD);
  if (!dash) dash = ss.insertSheet(CONFIG.SHEET_DASHBOARD);
  dash.clearContents();

  const deadSheet   = ss.getSheetByName(CONFIG.SHEET_DEAD);
  const expirySheet = ss.getSheetByName(CONFIG.SHEET_EXPIRY);
  const rawSheet    = ss.getSheetByName(CONFIG.SHEET_RAW);

  const deadCount   = deadSheet   ? Math.max(deadSheet.getLastRow()   - 1, 0) : 0;
  const expiryCount = expirySheet ? Math.max(expirySheet.getLastRow() - 1, 0) : 0;
  const rawCount    = rawSheet    ? Math.max(rawSheet.getLastRow()    - 1, 0) : 0;

  const kpis = [
    ["إجمالي الأصناف المُحلَّلة",    rawCount],
    ["أصناف راكدة (مبيعات = صفر)",   deadCount],
    ["أصناف قرب انتهاء الصلاحية",    expiryCount],
    ["آخر تحديث", Utilities.formatDate(new Date(),"Asia/Riyadh","yyyy-MM-dd HH:mm")],
  ];

  dash.getRange("A1").setValue("لوحة التحكم — Weekly Pharmacy Feedback Loop");
  dash.getRange("A1").setFontWeight("bold").setFontSize(14);
  dash.getRange("A3:B" + (2 + kpis.length)).setValues(kpis);
  styleHeaderRow(dash, 1, 2);

  // تسجيل هذا الأسبوع تلقائياً في السجل التاريخي
  appendToHistoricalLog(deadCount, expiryCount, rawSheet, deadSheet);
}

// ── السجل التاريخي — يُكمل تلقائياً كل أسبوع ──────────────────
function appendToHistoricalLog(deadCount, expiryCount, rawSheet, deadSheet) {
  const ss       = SpreadsheetApp.getActiveSpreadsheet();

  // إذا استُدعيت من القائمة (بدون معاملات) — اجلب كل شيء من الشيتات مباشرة
  if (deadCount === undefined) {
    rawSheet    = ss.getSheetByName(CONFIG.SHEET_RAW);
    deadSheet   = ss.getSheetByName(CONFIG.SHEET_DEAD);
    const expSh = ss.getSheetByName(CONFIG.SHEET_EXPIRY);
    deadCount   = deadSheet ? Math.max(deadSheet.getLastRow() - 1, 0) : 0;
    expiryCount = expSh    ? Math.max(expSh.getLastRow()     - 1, 0) : 0;
  }

  const weekDate = Utilities.formatDate(new Date(), "Asia/Riyadh", "yyyy-MM-dd");

  // إنشاء الشيت لو غير موجود
  let hist = ss.getSheetByName(CONFIG.SHEET_HISTORY);
  if (!hist) {
    hist = ss.insertSheet(CONFIG.SHEET_HISTORY);
    const headers = ["الأسبوع", "فروع أرسلت", "إجمالي الراكد (ر.س)", "أصناف راكدة", "قرب الانتهاء", "ملاحظة الأسبوع"];
    hist.appendRow(headers);
    styleHeaderRow(hist, 1, headers.length);
    hist.setColumnWidth(1, 110);
    hist.setColumnWidth(2, 100);
    hist.setColumnWidth(3, 150);
    hist.setColumnWidth(4, 110);
    hist.setColumnWidth(5, 120);
    hist.setColumnWidth(6, 260);
  }

  // حساب عدد الفروع الفريدة (عمود B في بيانات الفروع)
  let branchCount = 0;
  if (rawSheet && rawSheet.getLastRow() > 1) {
    const branches = rawSheet.getRange(2, 2, rawSheet.getLastRow() - 1, 1).getValues().flat();
    branchCount = new Set(branches.filter(b => String(b).trim() !== "")).size;
  }

  // حساب إجمالي قيمة الراكد (عمود 7 في شيت الراكد)
  let deadValue = 0;
  if (deadSheet && deadSheet.getLastRow() > 1) {
    deadSheet.getRange(2, 7, deadSheet.getLastRow() - 1, 1).getValues()
      .forEach(([v]) => { deadValue += parseFloat(v) || 0; });
  }

  const newRow = [weekDate, branchCount, +deadValue.toFixed(2), deadCount, expiryCount, ""];

  // هل هذا الأسبوع موجود بالفعل؟
  const lastRow = hist.getLastRow();
  if (lastRow > 1) {
    const weeks = hist.getRange(2, 1, lastRow - 1, 1).getValues().flat()
      .map(d => d instanceof Date
        ? Utilities.formatDate(d, "Asia/Riyadh", "yyyy-MM-dd")
        : String(d).trim());
    const existingIdx = weeks.indexOf(weekDate);

    if (existingIdx >= 0) {
      // تحديث الأعمدة 2-5 فقط — لا نمسح ملاحظة المستخدم في العمود 6
      const sheetRow = existingIdx + 2;
      hist.getRange(sheetRow, 2, 1, 4).setValues([[branchCount, +deadValue.toFixed(2), deadCount, expiryCount]]);
      logEntry(`السجل التاريخي: تم تحديث أسبوع ${weekDate} — فروع: ${branchCount} | راكد: ${deadCount} | انتهاء: ${expiryCount}`, "INFO");
      return;
    }
  }

  // أسبوع جديد — أضف صفاً
  hist.appendRow(newRow);

  // تلوين متبادل لسهولة القراءة
  const addedRow = hist.getLastRow();
  if (addedRow % 2 === 0) {
    hist.getRange(addedRow, 1, 1, 6).setBackground("#EBF2FA");
  }

  logEntry(`السجل التاريخي: تمت إضافة أسبوع ${weekDate} — فروع: ${branchCount} | راكد: ${deadCount} | انتهاء: ${expiryCount}`, "SUCCESS");
}

// ── التقرير الأسبوعي بالإيميل ────────────────────────────────
function sendWeeklyReport() {
  if (!CONFIG.EMAIL_ENABLED) return;

  const ss        = SpreadsheetApp.getActiveSpreadsheet();
  const sheetUrl  = ss.getUrl();
  const weekDate  = Utilities.formatDate(new Date(), "Asia/Riyadh", "yyyy-MM-dd");

  // ── جمع الأرقام من الشيتات ─────────────────────────────────
  const deadSheet   = ss.getSheetByName(CONFIG.SHEET_DEAD);
  const expirySheet = ss.getSheetByName(CONFIG.SHEET_EXPIRY);
  const rawSheet    = ss.getSheetByName(CONFIG.SHEET_RAW);
  const slowSheet   = ss.getSheetByName(CONFIG.SHEET_SLOW);
  const fastSheet   = ss.getSheetByName(CONFIG.SHEET_FAST);

  const deadCount   = deadSheet   ? Math.max(deadSheet.getLastRow()   - 1, 0) : 0;
  const expiryCount = expirySheet ? Math.max(expirySheet.getLastRow() - 1, 0) : 0;
  const totalItems  = rawSheet    ? Math.max(rawSheet.getLastRow()    - 1, 0) : 0;
  const slowCount   = slowSheet   ? Math.max(slowSheet.getLastRow()   - 1, 0) : 0;
  const fastCount   = fastSheet   ? Math.max(fastSheet.getLastRow()   - 1, 0) : 0;

  // حساب إجمالي قيمة المخزون الراكد (عمود G في شيت الراكد)
  let deadValue = 0;
  if (deadSheet && deadSheet.getLastRow() > 1) {
    const valueCol = deadSheet.getRange(2, 7, deadSheet.getLastRow() - 1, 1).getValues();
    valueCol.forEach(([v]) => { deadValue += parseFloat(v) || 0; });
  }

  // أعلى 5 أصناف راكدة بالقيمة
  let topDeadRows = [];
  if (deadSheet && deadSheet.getLastRow() > 1) {
    const data = deadSheet.getRange(2, 1, deadSheet.getLastRow() - 1, 8).getValues();
    topDeadRows = data
      .map(r => ({ week: r[0], branch: r[1], name: r[2], sku: r[3], stock: r[4], price: r[5], value: parseFloat(r[6]) || 0, reason: r[7] }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 5);
  }

  // أقرب 5 أصناف لانتهاء الصلاحية
  let topExpiryRows = [];
  if (expirySheet && expirySheet.getLastRow() > 1) {
    const data = expirySheet.getRange(2, 1, expirySheet.getLastRow() - 1, 9).getValues();
    topExpiryRows = data
      .map(r => ({ branch: r[1], name: r[2], sku: r[3], stock: r[4], expiry: r[5], daysLeft: parseInt(r[6]) || 999, urgency: r[7], action: r[8] }))
      .filter(r => r.daysLeft < 999)
      .sort((a, b) => a.daysLeft - b.daysLeft)
      .slice(0, 5);
  }

  // ── بناء جسم الإيميل (HTML) ────────────────────────────────
  const deadValueFmt = deadValue.toLocaleString("ar-SA", { style: "currency", currency: "SAR", maximumFractionDigits: 0 });

  // صفوف جدول الراكد
  const deadTableRows = topDeadRows.length
    ? topDeadRows.map(r =>
        `<tr>
          <td style="padding:6px 10px;border-bottom:1px solid #e9ecef">${r.branch}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e9ecef">${r.name}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e9ecef;text-align:right;font-weight:bold;color:#c0392b">${(r.value).toLocaleString("ar-SA")} ر.س</td>
        </tr>`).join("")
    : `<tr><td colspan="4" style="padding:10px;text-align:center;color:#6c757d">لا توجد أصناف راكدة هذا الأسبوع ✅</td></tr>`;

  // صفوف جدول الصلاحية
  const expiryTableRows = topExpiryRows.length
    ? topExpiryRows.map(r => {
        const urgencyColor = r.daysLeft <= 30 ? "#c0392b" : "#e67e22";
        const urgencyBg    = r.daysLeft <= 30 ? "#fdf0f0" : "#fef9f0";
        return `<tr style="background:${urgencyBg}">
          <td style="padding:6px 10px;border-bottom:1px solid #e9ecef">${r.branch}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e9ecef">${r.name}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e9ecef;text-align:center">${r.expiry instanceof Date ? Utilities.formatDate(r.expiry,"Asia/Riyadh","dd/MM/yyyy") : r.expiry}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e9ecef;text-align:center;font-weight:bold;color:${urgencyColor}">${r.daysLeft} يوم</td>
          <td style="padding:6px 10px;border-bottom:1px solid #e9ecef;font-size:12px">${r.action}</td>
        </tr>`;}).join("")
    : `<tr><td colspan="5" style="padding:10px;text-align:center;color:#6c757d">لا توجد أصناف قريبة من الانتهاء ✅</td></tr>`;

  const htmlBody = `<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:Tahoma,Arial,sans-serif;direction:rtl">
<div style="max-width:680px;margin:24px auto;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">
  <div style="background:#1D3557;padding:28px 32px;text-align:center">
    <h1 style="margin:0;color:#fff;font-size:22px">📊 التقرير الأسبوعي — نظام الصيدليات</h1>
    <p style="margin:6px 0 0;color:#a8c4e0;font-size:14px">أسبوع ${weekDate}</p>
  </div>
  <div style="display:flex;background:#f8f9fa;border-bottom:3px solid #1D3557;padding:20px 32px;gap:16px;flex-wrap:wrap">
    <div style="flex:1;min-width:120px;text-align:center;background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)">
      <div style="font-size:28px;font-weight:bold;color:#1D3557">${totalItems.toLocaleString()}</div>
      <div style="font-size:12px;color:#6c757d;margin-top:4px">إجمالي الأصناف</div>
    </div>
    <div style="flex:1;min-width:120px;text-align:center;background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)">
      <div style="font-size:28px;font-weight:bold;color:#c0392b">${deadCount.toLocaleString()}</div>
      <div style="font-size:12px;color:#6c757d;margin-top:4px">صنف راكد</div>
    </div>
    <div style="flex:1;min-width:120px;text-align:center;background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)">
      <div style="font-size:22px;font-weight:bold;color:#c0392b">${deadValueFmt}</div>
      <div style="font-size:12px;color:#6c757d;margin-top:4px">قيمة الراكد</div>
    </div>
    <div style="flex:1;min-width:120px;text-align:center;background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)">
      <div style="font-size:28px;font-weight:bold;color:#e67e22">${expiryCount.toLocaleString()}</div>
      <div style="font-size:12px;color:#6c757d;margin-top:4px">قرب انتهاء صلاحية</div>
    </div>
    <div style="flex:1;min-width:120px;text-align:center;background:#fff;border-radius:8px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)">
      <div style="font-size:28px;font-weight:bold;color:#27ae60">${fastCount.toLocaleString()}</div>
      <div style="font-size:12px;color:#6c757d;margin-top:4px">سريع الحركة</div>
    </div>
  </div>
  <div style="padding:28px 32px">
    <h2 style="color:#c0392b;font-size:16px;margin:0 0 12px;border-right:4px solid #c0392b;padding-right:10px">🔴 أعلى الأصناف الراكدة — يحتاج إجراء تسويقي فوري</h2>
    <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px;margin-bottom:28px">
      <thead>
        <tr style="background:#1D3557;color:#fff">
          <th style="padding:8px 10px;text-align:right">الفرع</th>
          <th style="padding:8px 10px;text-align:right">اسم الصنف</th>
          <th style="padding:8px 10px;text-align:center">الرصيد</th>
          <th style="padding:8px 10px;text-align:right">القيمة</th>
        </tr>
      </thead>
      <tbody>${deadTableRows}</tbody>
    </table>
    <h2 style="color:#e67e22;font-size:16px;margin:0 0 12px;border-right:4px solid #e67e22;padding-right:10px">🟡 أصناف قرب انتهاء الصلاحية — الأكثر إلحاحاً</h2>
    <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px;margin-bottom:28px">
      <thead>
        <tr style="background:#1D3557;color:#fff">
          <th style="padding:8px 10px;text-align:right">الفرع</th>
          <th style="padding:8px 10px;text-align:right">اسم الصنف</th>
          <th style="padding:8px 10px;text-align:center">تاريخ الانتهاء</th>
          <th style="padding:8px 10px;text-align:center">أيام متبقية</th>
          <th style="padding:8px 10px;text-align:right">الإجراء</th>
        </tr>
      </thead>
      <tbody>${expiryTableRows}</tbody>
    </table>
    <div style="background:#f8f9fa;border-radius:8px;padding:14px 18px;font-size:13px;color:#495057;margin-bottom:28px">
      📦 بطيء الحركة: <strong>${slowCount}</strong> صنف &nbsp;|&nbsp;
      🚀 سريع الحركة: <strong>${fastCount}</strong> صنف &nbsp;|&nbsp;
      راجع التفاصيل الكاملة في لوحة التحكم
    </div>
    <div style="text-align:center;margin-bottom:8px">
      <a href="${sheetUrl}" style="display:inline-block;background:#1D3557;color:#fff;text-decoration:none;padding:12px 32px;border-radius:6px;font-size:14px;font-weight:bold">🔗 افتح لوحة التحكم الكاملة</a>
    </div>
  </div>
  <div style="background:#f8f9fa;border-top:1px solid #dee2e6;padding:14px 32px;text-align:center;font-size:11px;color:#adb5bd">
    تم إرسال هذا التقرير تلقائياً بواسطة نظام الصيدليات — Weekly Pharmacy Feedback Loop<br>
    ${weekDate} | للإيقاف: اجعل CONFIG.EMAIL_ENABLED = false
  </div>
</div>
</body>
</html>`;

  const plainText =
    `التقرير الأسبوعي — نظام الصيدليات (${weekDate})\n` +
    `${"─".repeat(45)}\n` +
    `إجمالي الأصناف المحللة : ${totalItems}\n` +
    `أصناف راكدة            : ${deadCount}\n` +
    `قيمة المخزون الراكد    : ${deadValueFmt}\n` +
    `قرب انتهاء الصلاحية   : ${expiryCount}\n` +
    `بطيء الحركة            : ${slowCount}\n` +
    `سريع الحركة            : ${fastCount}\n` +
    `${"─".repeat(45)}\n` +
    `لوحة التحكم: ${sheetUrl}`;

  const subject = `📊 تقرير الصيدليات الأسبوعي — ${weekDate} | راكد: ${deadCount} صنف | قيمة: ${deadValueFmt}`;
  const recipients = [CONFIG.EMAIL_MARKETING, CONFIG.EMAIL_PURCHASING].join(",");

  try {
    GmailApp.sendEmail(recipients, subject, plainText, { htmlBody, name: "نظام الصيدليات" });
    logEntry(`تم إرسال التقرير الأسبوعي إلى: ${recipients}`, "SUCCESS");
  } catch (err) {
    logEntry(`فشل إرسال الإيميل: ${err.message}`, "ERROR");
  }
}

// ── إنشاء Google Form للفروع ──────────────────────────────────
function createBranchForm() {
  const ss  = SpreadsheetApp.getActiveSpreadsheet();
  const ui  = SpreadsheetApp.getUi();

  // ─── إنشاء النموذج ───────────────────────────────────────────
  const form = FormApp.create("نموذج التغذية الراجعة الأسبوعي — الصيدليات");
  form.setDescription("يُملأ كل أحد قبل الساعة 12 ظهراً. يستغرق أقل من 5 دقائق.");
  form.setConfirmationMessage("شكراً! تم استلام تقريرك بنجاح ✅");
  form.setProgressBar(true);
  form.setShowLinkToRespondAgain(false);

  // ─── القسم 1: معلومات الفرع (→ أعمدة B C D) ─────────────────
  form.addSectionHeaderItem()
      .setTitle("القسم الأول: معلومات الفرع")
      .setHelpText("يرجى تعبئة هذه الحقول بدقة — تستخدم لتحديد الفرع في التقارير");

  form.addListItem()
      .setTitle("اسم الفرع")
      .setRequired(true)
      .setChoiceValues([
        "الفرع الرئيسي","النزهة","العليا","الروضة","الحمراء",
        "المروج","السليمانية","حي الملك فهد","الملك عبدالله",
        "العزيزية","الفرع 11","الفرع 12","الفرع 13"
      ]);

  form.addTextItem()
      .setTitle("اسم الصيدلي المداوم الآن")
      .setRequired(true);

  form.addMultipleChoiceItem()
      .setTitle("كيف كان الأسبوع بشكل عام؟")
      .setRequired(true)
      .setChoiceValues(["ممتاز","جيد","متوسط","ضعيف"]);

  // ─── القسم 2: الأصناف الناقصة (→ أعمدة E F G H I J) ─────────
  form.addPageBreakItem()
      .setTitle("القسم الثاني: الأصناف الناقصة")
      .setHelpText("أصناف طلبها عملاء ولم تجدها في المخزون هذا الأسبوع");

  form.addTextItem().setTitle("الصنف الناقص الأول");
  form.addTextItem().setTitle("كم مرة طلب؟ (الصنف الأول)").setHelpText("اكتب عدداً أو اتركه فارغاً");
  form.addTextItem().setTitle("الصنف الناقص الثاني");
  form.addTextItem().setTitle("كم مرة طلب؟ (الصنف الثاني)");
  form.addTextItem().setTitle("الصنف الناقص الثالث");
  form.addTextItem().setTitle("كم مرة طلب؟ (الصنف الثالث)");

  // ─── القسم 3: الأصناف الراكدة (→ أعمدة K L M) ───────────────
  form.addPageBreakItem()
      .setTitle("القسم الثالث: الأصناف الراكدة")
      .setHelpText("أصناف موجودة في الصيدلية منذ فترة ولا تتحرك");

  form.addTextItem().setTitle("صنف راكد لا يتحرك").setHelpText("اكتب اسمه أو اتركه فارغاً");
  form.addTextItem().setTitle("كم وحدة موجودة منه؟");
  form.addTextItem().setTitle("قيمته التقريبية بالريال");

  // ─── القسم 4: قرب انتهاء الصلاحية (→ أعمدة N O) ─────────────
  form.addPageBreakItem()
      .setTitle("القسم الرابع: قرب انتهاء الصلاحية")
      .setHelpText("أصناف ستنتهي صلاحيتها قريباً وتحتاج تصريف أو إرجاع");

  form.addTextItem().setTitle("اسم الصنف قرب انتهاء الصلاحية").setHelpText("اتركه فارغاً إن لم يوجد");
  form.addDateItem().setTitle("تاريخ انتهاء الصلاحية").setIncludesYear(true);

  // ─── القسم 5: طلبات العملاء والملاحظات (→ أعمدة P Q R) ───────
  form.addPageBreakItem()
      .setTitle("القسم الخامس: طلبات العملاء والملاحظات")
      .setHelpText("هذه المعلومات لا يعرفها النظام — رأيك هنا يصنع الفرق");

  form.addParagraphTextItem()
      .setTitle("ماذا يطلب العملاء ولا نحمله؟")
      .setHelpText("أي صنف يسأل عنه العملاء ولا يجدونه");

  form.addParagraphTextItem()
      .setTitle("أي ملاحظات أخرى تريد تبليغها؟")
      .setHelpText("شكاوى متكررة، مشكلة موردين، أي معلومة تراها مهمة");

  form.addParagraphTextItem()
      .setTitle("أداء العروض هذا الأسبوع")
      .setHelpText("هل العملاء يلاحظون العروض؟ أي عرض يعمل أو لا يعمل؟");

  // ─── ربط النموذج بالـ Spreadsheet ───────────────────────────
  form.setDestination(FormApp.DestinationType.SPREADSHEET, ss.getId());
  Utilities.sleep(3000);

  // إعادة تسمية شيت الردود إلى "بيانات الفروع"
  const responseSheet = ss.getSheets().find(s =>
    /form responses|ردود النماذج|نماذج/i.test(s.getName())
  );
  if (responseSheet) {
    const existing = ss.getSheetByName(CONFIG.SHEET_RAW);
    if (existing) existing.setName(CONFIG.SHEET_RAW + " (يدوي)");
    responseSheet.setName(CONFIG.SHEET_RAW);
    logEntry("تمت إعادة تسمية شيت الردود إلى " + CONFIG.SHEET_RAW, "INFO");
  }

  // ─── أظهر الروابط للمستخدم ───────────────────────────────────
  const shareUrl = form.getPublishedUrl();
  const editUrl  = form.getEditUrl();
  logEntry("تم إنشاء Google Form: " + shareUrl, "SUCCESS");

  ui.alert(
    "تم إنشاء النموذج بنجاح!\n\n" +
    "رابط النموذج (أرسله للفروع):\n" + shareUrl + "\n\n" +
    "رابط التعديل (للمدير فقط):\n"  + editUrl  + "\n\n" +
    "الردود ستظهر في شيت: " + CONFIG.SHEET_RAW + "\n" +
    "A=Timestamp | B=الفرع | C=الصيدلي | D=التقييم\n" +
    "E-J=الناقص*3 | K-M=الراكد | N-O=الانتهاء | P-R=ملاحظات"
  );
}

// ── مساعدات ───────────────────────────────────────────────────
function importExcelAsTemp(file) {
  try {
    const blob   = file.getBlob();
    const tempSS = SpreadsheetApp.openById(
      Drive.Files.insert({title:"__temp__",mimeType:MimeType.GOOGLE_SHEETS},blob).id
    );
    return tempSS.getSheets()[0];
  } catch(e) {
    logEntry("خطأ في استيراد Excel: " + e.message, "ERROR");
    return null;
  }
}

function findColIndex(headers, candidates) {
  for (const c of candidates) {
    const i = headers.findIndex(h => h.includes(c.toLowerCase()));
    if (i >= 0) return i;
  }
  return -1;
}

function parseNum(val) {
  if (val === null || val === undefined || val === "") return 0;
  const n = parseFloat(String(val).replace(/[^0-9.\-]/g,""));
  return isNaN(n) ? 0 : n;
}

function parseDate(val) {
  if (!val) return null;
  if (val instanceof Date) return isNaN(val.getTime()) ? null : val;
  const d = new Date(val);
  return isNaN(d.getTime()) ? null : d;
}

function extractBranchName(fileName) {
  return fileName.replace(/\.(xlsx?|csv)$/i,"")
                 .replace(/_?\d{4}-\d{2}-\d{2}/,"")
                 .replace(/_/g," ").trim() || "فرع غير محدد";
}

function getOrCreateFolder(name) {
  const folders = DriveApp.getFoldersByName(name);
  return folders.hasNext() ? folders.next() : DriveApp.createFolder(name);
}

function logEntry(message, level) {
  const ss    = SpreadsheetApp.getActiveSpreadsheet();
  let   sheet = ss.getSheetByName(CONFIG.SHEET_LOG);
  if (!sheet) {
    sheet = ss.insertSheet(CONFIG.SHEET_LOG);
    sheet.appendRow(["التاريخ والوقت","المستوى","الرسالة"]);
    styleHeaderRow(sheet, 1, 3);
  }
  const time = Utilities.formatDate(new Date(),"Asia/Riyadh","yyyy-MM-dd HH:mm:ss");
  sheet.appendRow([time, level, message]);
}

function styleHeaderRow(sheet, row, cols) {
  const range = sheet.getRange(row, 1, 1, cols);
  range.setBackground("#1D3557")
       .setFontColor("#FFFFFF")
       .setFontWeight("bold")
       .setHorizontalAlignment("center");
  sheet.setFrozenRows(row);
}

// ── الإعداد الأولي ────────────────────────────────────────────
function setupTrigger() {
  const folder = getOrCreateFolder(CONFIG.UPLOAD_FOLDER_NAME);
  const ss     = SpreadsheetApp.getActiveSpreadsheet();

  // ── 1. إنشاء الشيتات ─────────────────────────────────────────
  [CONFIG.SHEET_RAW, CONFIG.SHEET_DEAD, CONFIG.SHEET_EXPIRY,
   CONFIG.SHEET_SLOW, CONFIG.SHEET_FAST, CONFIG.SHEET_DASHBOARD,
   CONFIG.SHEET_LOG, CONFIG.SHEET_HISTORY
  ].forEach(name => { if (!ss.getSheetByName(name)) ss.insertSheet(name); });

  // ── 2. حماية من التكرار — احذف أي مشغلات سابقة أولاً ──────────
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));

  // ── 3. مشغل أسبوعي — كل أحد الساعة 9 صباحاً ─────────────────
  // يعالج جميع ملفات الفروع المرفوعة خلال الأسبوع دفعةً واحدة
  ScriptApp.newTrigger("processAllPendingFiles")
    .timeBased()
    .onWeekDay(ScriptApp.WeekDay.SUNDAY)
    .atHour(9)
    .nearMinute(0)
    .create();

  // ── 4. مشغل ساعي — بديل عملي عن Drive folder trigger ──────────
  // Apps Script لا يدعم trigger مباشر على مجلد Drive بعينه،
  // لذا نفحص المجلد كل ساعة ونعالج فور وجود ملف جديد.
  ScriptApp.newTrigger("checkForNewFiles")
    .timeBased()
    .everyHours(1)
    .create();

  logEntry("تم إنشاء المشغلات: أسبوعي (الأحد 9ص) + فحص ساعي", "SUCCESS");

  SpreadsheetApp.getUi().alert(
    "الإعداد اكتمل!\n\n" +
    "رابط مجلد الرفع:\n" + folder.getUrl() + "\n\n" +
    "المشغلات التلقائية:\n" +
    "  الأحد الساعة 9 صباحاً — معالجة جميع الفروع\n" +
    "  كل ساعة               — فحص الملفات الجديدة\n\n" +
    "أرسل رابط المجلد لمدراء الفروع.\n" +
    "ملفاتهم تُعالَج خلال ساعة من الرفع — أو في الأحد 9ص على أبطأ تقدير."
  );
}

// ── فحص ساعي — يعوّض غياب Drive folder trigger ────────────────
// Apps Script لا يوفر trigger خاص بمجلد Drive — هذا الفحص الدوري
// يحقق نفس النتيجة: يشغّل المعالجة فور اكتشاف أي ملف جديد.
function checkForNewFiles() {
  const folder = getOrCreateFolder(CONFIG.UPLOAD_FOLDER_NAME);
  const files  = folder.getFilesByType(MimeType.MICROSOFT_EXCEL);
  let   found  = false;
  while (files.hasNext()) {
    if (!files.next().getName().startsWith("✓")) { found = true; break; }
  }
  if (found) {
    logEntry("checkForNewFiles: ملفات جديدة — بدء المعالجة التلقائية", "INFO");
    processAllPendingFiles();
  }
}

// ── قائمة مخصصة في الـ Sheet ──────────────────────────────────
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("نظام الصيدليات")
    .addItem("معالجة الملفات الجديدة",                "processAllPendingFiles")
    .addItem("تحديث لوحة التحكم",                     "refreshDashboard")
    .addItem("إرسال التقرير الأسبوعي الآن",           "sendWeeklyReport")
    .addItem("تسجيل هذا الأسبوع في السجل التاريخي",  "appendToHistoricalLog")
    .addSeparator()
    .addItem("إنشاء نموذج Google Form للفروع",        "createBranchForm")
    .addItem("إعداد النظام (مرة واحدة)",              "setupTrigger")
    .addToUi();
}
