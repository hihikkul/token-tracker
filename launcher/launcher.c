/* TokenTracker.exe: a tiny launcher kept next to app.py.
 *
 * It starts the Python app in this folder (pythonw, no console window) and exits, so the
 * app itself stays plain Python files that update themselves. Pinning or double-clicking
 * TokenTracker.exe therefore always runs the newest version. Build (any OS):
 *   x86_64-w64-mingw32-windres launcher.rc -O coff -o launcher.res
 *   x86_64-w64-mingw32-gcc -O2 -municode -mwindows launcher.c launcher.res -o ../TokenTracker.exe
 */
#include <windows.h>
#include <shlwapi.h>
#include <wchar.h>

static int exists(const wchar_t *p) { DWORD a = GetFileAttributesW(p); return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY); }

/* pythonw on PATH (skipping the Microsoft Store stub when a real one exists), then Anaconda, then the py launcher */
static int find_python(wchar_t *out, size_t n, int *use_py_launcher) {
    wchar_t buf[MAX_PATH];
    *use_py_launcher = 0;
    if (SearchPathW(NULL, L"pythonw.exe", NULL, MAX_PATH, buf, NULL) && !wcsstr(buf, L"WindowsApps")) { wcsncpy(out, buf, n); return 1; }
    const wchar_t *roots[] = { L"%USERPROFILE%\\anaconda3\\pythonw.exe", L"%USERPROFILE%\\miniconda3\\pythonw.exe",
                               L"%LOCALAPPDATA%\\Programs\\Python\\Python313\\pythonw.exe",
                               L"%LOCALAPPDATA%\\Programs\\Python\\Python312\\pythonw.exe",
                               L"%LOCALAPPDATA%\\Programs\\Python\\Python311\\pythonw.exe",
                               L"%LOCALAPPDATA%\\Programs\\Python\\Python310\\pythonw.exe" };
    for (int i = 0; i < (int)(sizeof roots / sizeof *roots); i++) {
        if (ExpandEnvironmentStringsW(roots[i], buf, MAX_PATH) && exists(buf)) { wcsncpy(out, buf, n); return 1; }
    }
    if (SearchPathW(NULL, L"pyw.exe", NULL, MAX_PATH, buf, NULL)) { wcsncpy(out, buf, n); *use_py_launcher = 1; return 1; }
    if (SearchPathW(NULL, L"pythonw.exe", NULL, MAX_PATH, buf, NULL)) { wcsncpy(out, buf, n); return 1; }
    return 0;
}

int WINAPI wWinMain(HINSTANCE h, HINSTANCE p, PWSTR args, int show) {
    wchar_t dir[MAX_PATH], app[MAX_PATH], py[MAX_PATH], cmd[4 * MAX_PATH];
    int pyl;
    GetModuleFileNameW(NULL, dir, MAX_PATH);
    PathRemoveFileSpecW(dir);
    swprintf(app, MAX_PATH, L"%ls\\app.py", dir);
    if (!exists(app)) {
        MessageBoxW(NULL, L"app.py was not found next to TokenTracker.exe.\nKeep the exe inside the token-tracker folder.", L"Token Tracker", MB_ICONERROR);
        return 1;
    }
    if (!find_python(py, MAX_PATH, &pyl)) {
        MessageBoxW(NULL, L"Python 3.10+ was not found.\nInstall it from python.org (tick \"Add python.exe to PATH\"), then try again.", L"Token Tracker", MB_ICONERROR);
        ShellExecuteW(NULL, L"open", L"https://www.python.org/downloads/", NULL, NULL, SW_SHOWNORMAL);
        return 1;
    }
    swprintf(cmd, 4 * MAX_PATH, pyl ? L"\"%ls\" -3 \"%ls\" %ls" : L"\"%ls\" \"%ls\" %ls", py, app, args ? args : L"");
    STARTUPINFOW si = { sizeof si };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"Could not start Python.", L"Token Tracker", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
