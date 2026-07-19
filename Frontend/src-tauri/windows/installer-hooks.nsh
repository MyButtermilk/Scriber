!macro NSIS_HOOK_POSTINSTALL
  ${If} ${FileExists} "$INSTDIR\backend\_internal\nltk_data\tokenizers\punkt_tab.zip"
    Delete "$INSTDIR\backend\_internal\nltk_data\tokenizers\punkt_tab.zip"
  ${EndIf}
!macroend
