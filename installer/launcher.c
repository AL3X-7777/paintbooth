/* Paintbooth.exe - starts the bundled Python runtime with the Paintbooth app, without a console window.
 *
 * Installed layout (see setup.c and build_installer.py):
 *     <folder>\Paintbooth.exe            this program
 *     <folder>\runtime\pythonw.exe        bundled CPython (no console)
 *     <folder>\app\Paintbooth.pyw        the app's start script
 *
 * It runs  "<folder>\runtime\pythonw.exe" "<folder>\app\Paintbooth.pyw" <arguments>
 * with <folder> as the working directory and exits at once. The arguments (for example an .ms2
 * or .pb file from "Open with" or a double-click) are passed through unchanged, quotes included.
 * The icon and version information come from launcher.rc, written by build_installer.py. */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

/* Entry point. Returns 0 if Python was started, 1 if the files are missing. */
int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, PWSTR args, int show)
{
    (void)hInst; (void)hPrev; (void)show;
    static wchar_t dir[MAX_PATH * 2];
    static wchar_t cmd[32768];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH * 2);
    if (n == 0 || n >= MAX_PATH * 2) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;                     /* dir = the folder this .exe is in */

    _snwprintf(cmd, 32767, L"\"%ls\\runtime\\pythonw.exe\" \"%ls\\app\\Paintbooth.pyw\" %ls", dir, dir, args ? args : L"");
    cmd[32767] = 0;                            /* 32767 = the CreateProcess command-line limit */

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"Paintbooth could not start because its files are missing or damaged.\n\n"
                          L"Please run the Paintbooth installer again.", L"Paintbooth", MB_ICONERROR | MB_OK);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
