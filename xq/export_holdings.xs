// 持股匯出測試（XQ 自動交易中心，免費版可用）
// 用途：把「這檔商品在交易帳號中的實際庫存」寫到檔案，給投資擂台研究台讀取。
// 設定：頻率選「日」；交易帳號選你的競賽帳號；商品放你的持股（免費版每個策略最多 5 檔）。
// 本腳本只讀取庫存，不會下任何單（沒有 SetPosition）。
var: intrabarpersist printed(false);

if currenttime >= 090000 and not printed then begin
    print(file("C:\SysJust\XQLite\XS\Print\holdings_[scriptname].log"),
          numtostr(currentdate, 0), numtostr(currenttime, 0), symbol, numtostr(FilledAtBroker, 0));
    printed = true;
end;
