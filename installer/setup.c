/* Paintbooth installer (Setup.exe).
 *
 * Payload layout
 * --------------
 * build_installer.py appends the program files to this compiled stub:
 *
 *     [ PE image of this stub ][ ZIP archive ][ "SKYPAYLD" ][ uint64 LE: file offset of the ZIP ]
 *                                              \________ 16-byte trailer _______________________/
 *
 * The ZIP holds the "Paintbooth" folder: Paintbooth.exe (launcher.c), runtime\ (a trimmed
 * CPython with Pillow and numpy) and app\ (the paintbooth package, winsetup.py and the icon).
 * Authenticode signing appends the certificate table *behind* the trailer (8-byte aligned) and
 * records its offset in the PE security directory, so trailer_end() also looks just before that
 * table. Do not change this layout: signed releases and build_installer.find_payload rely on it.
 *
 * Install steps (wWinMain)
 * ------------------------
 *  1. Parse the command line (parse_args): /S silent, /DESKTOP desktop icon, /D=<folder> install
 *     folder. As in NSIS, /D= must be the last argument and takes the rest of the line unquoted.
 *  2. Pick the folder: /D=, else the previous install's InstallLocation (so updates land in the
 *     same place), else %LOCALAPPDATA%\Programs\Paintbooth. A folder the user picks always gets
 *     its own "Paintbooth" subfolder (normalise_dir).
 *  3. Unless /S: show the options window (folder, Browse..., desktop icon).
 *  4. Read this .exe into memory, delete the old runtime\ and app\ folders (never anything
 *     else, and never Documents\Paintbooth), and unpack the payload. Entry names are checked
 *     (safe_name): no absolute paths, drive letters or ".." parts.
 *  5. Run "runtime\pythonw.exe app\winsetup.py install [--desktop]", which creates the
 *     shortcuts, the uninstall entry, the file types and Documents\Paintbooth.
 *  6. Unless /S: offer to start Paintbooth.
 *
 * Everything is per user (HKCU, %LOCALAPPDATA%): no admin rights are needed.
 *
 * Build (see build_installer.py for the exact flags):
 *     zig cc -target x86_64-windows-gnu -O2 -municode -Wl,--subsystem,windows setup.c miniz.c setup.rc
 * The portable core (payload location, name checks, extraction) also builds on Linux with
 * -DSKY_TEST into a small harness: "harness Setup.exe outdir" unpacks the payload.
 */
#include "miniz.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TRAILER_MAGIC "SKYPAYLD"

/* ---------------------------------------------------------------- portable core */

/* write(ctx, name, data, size, is_dir): create one entry; returns non-zero on failure. */
typedef int (*write_fn)(void *ctx, const char *utf8_path, const void *data, size_t size, int is_dir);
/* progress(ctx, done, total): report extraction progress. */
typedef void (*progress_fn)(void *ctx, unsigned done, unsigned total);

/* Where the 16-byte trailer ends. Normally at the end of the file; after Authenticode signing the
 * certificate table is appended behind it (8-byte aligned), so look just before that table. */
static size_t trailer_end(const unsigned char *exe, size_t n)
{
    if (n >= 16 && memcmp(exe + n - 16, TRAILER_MAGIC, 8) == 0) return n;
    if (n < 0x40) return 0;
    uint32_t pe = exe[0x3c] | exe[0x3d] << 8 | exe[0x3e] << 16 | (uint32_t)exe[0x3f] << 24;
    if ((size_t)pe + 24 + 2 > n || memcmp(exe + pe, "PE\0\0", 4) != 0) return 0;
    size_t opt = pe + 24;
    unsigned magic = exe[opt] | exe[opt + 1] << 8;
    size_t dir = opt + (magic == 0x20b ? 112 : 96) + 4 * 8;          /* IMAGE_DIRECTORY_ENTRY_SECURITY */
    if (dir + 8 > n) return 0;
    uint32_t cert = exe[dir] | exe[dir + 1] << 8 | exe[dir + 2] << 16 | (uint32_t)exe[dir + 3] << 24;
    if (cert < 16 || cert > n) return 0;
    for (size_t end = cert; end >= 16 && end + 32 >= cert; end--)      /* skip the alignment padding */
        if (memcmp(exe + end - 16, TRAILER_MAGIC, 8) == 0) return end;
    return 0;
}

/* Is a ZIP entry name safe to create below the install folder?
 * Rejects empty names, absolute paths ("/x", "\\x"), anything with a drive or stream colon
 * ("C:x", "a:b") and any ".." path component. The payload is built by us, so this is defence in
 * depth against a damaged or tampered installer writing outside the chosen folder. */
static int safe_name(const char *name)
{
    if (!name || !*name || name[0] == '/' || name[0] == '\\') return 0;
    if (strchr(name, ':')) return 0;
    const char *part = name;
    for (const char *p = name;; p++) {
        if (*p == '/' || *p == '\\' || *p == 0) {
            if (p - part == 2 && part[0] == '.' && part[1] == '.') return 0;
            if (*p == 0) break;
            part = p + 1;
        }
    }
    return 1;
}

/* Find the ZIP payload in the installer image and hand every entry to write().
 * progress() (may be NULL) is called every 40 entries and at the end.
 * Returns 0 on success, otherwise a short error code shown to the user:
 *   1 no trailer, 2 bad offset, 3 not a ZIP, 4 bad entry, 5 folder not created,
 *   6 entry not decompressed, 7 file not written, 8 unsafe entry name. */
static int extract_payload(const unsigned char *exe, size_t exe_size, write_fn write, progress_fn progress, void *ctx)
{
    exe_size = trailer_end(exe, exe_size);
    if (exe_size < 16) return 1;
    uint64_t off = 0;
    for (int i = 0; i < 8; i++) off |= (uint64_t)exe[exe_size - 8 + i] << (8 * i);
    if (off >= exe_size - 16) return 2;
    const unsigned char *zipdata = exe + off;
    size_t zipsize = (size_t)(exe_size - 16 - off);

    mz_zip_archive zip;
    memset(&zip, 0, sizeof zip);
    if (!mz_zip_reader_init_mem(&zip, zipdata, zipsize, 0)) return 3;
    unsigned total = mz_zip_reader_get_num_files(&zip);
    for (unsigned i = 0; i < total; i++) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, i, &st)) { mz_zip_reader_end(&zip); return 4; }
        if (!safe_name(st.m_filename)) { mz_zip_reader_end(&zip); return 8; }
        if (st.m_is_directory) {
            if (write(ctx, st.m_filename, NULL, 0, 1)) { mz_zip_reader_end(&zip); return 5; }
        } else {
            size_t sz = 0;
            void *p = mz_zip_reader_extract_to_heap(&zip, i, &sz, 0);
            if (!p && st.m_uncomp_size) { mz_zip_reader_end(&zip); return 6; }
            int rc = write(ctx, st.m_filename, p ? p : "", sz, 0);
            mz_free(p);
            if (rc) { mz_zip_reader_end(&zip); return 7; }
        }
        if (progress && (i % 40 == 0 || i + 1 == total)) progress(ctx, i + 1, total);
    }
    mz_zip_reader_end(&zip);
    return 0;
}

#ifdef SKY_TEST
/* ---------------------------------------------------------------- Linux test harness */
#include <sys/stat.h>
#include <errno.h>

/* Create every parent folder of path (POSIX). */
static void mkdirs(char *path)
{
    for (char *p = path + 1; *p; p++) {
        if (*p == '/') { *p = 0; mkdir(path, 0755); *p = '/'; }
    }
}

/* write_fn for the harness: ctx is the output folder. */
static int lx_write(void *ctx, const char *name, const void *data, size_t size, int is_dir)
{
    char full[4096];
    int len = snprintf(full, sizeof full, "%s/%s", (const char *)ctx, name);
    if (len < 0 || (size_t)len >= sizeof full) return 1;
    mkdirs(full);
    if (is_dir) { mkdir(full, 0755); return 0; }
    FILE *f = fopen(full, "wb");
    if (!f) return 1;
    size_t w = fwrite(data, 1, size, f);
    fclose(f);
    return w != size;
}

/* harness: setup.exe outdir -> unpack the payload, print and return the extract code */
int main(int argc, char **argv)
{
    if (argc < 3) { fprintf(stderr, "usage: %s setup.exe outdir\n", argv[0]); return 2; }
    FILE *f = fopen(argv[1], "rb");
    if (!f) return 3;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    unsigned char *buf = n > 0 ? malloc((size_t)n) : NULL;
    if (!buf) { fclose(f); return 4; }
    if (fread(buf, 1, (size_t)n, f) != (size_t)n) { fclose(f); free(buf); return 4; }
    fclose(f);
    int rc = extract_payload(buf, (size_t)n, lx_write, NULL, argv[2]);
    free(buf);
    printf("extract rc=%d\n", rc);
    return rc;
}

#else
/* ---------------------------------------------------------------- Windows installer */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shlobj.h>
#include <shellapi.h>
#include <wchar.h>
#include <stdarg.h>

#define DIR_CAP (MAX_PATH * 2)                 /* capacity of g_dir, in wchar_t */
static wchar_t g_dir[DIR_CAP];                 /* the install folder */
static HWND g_win, g_label, g_edit, g_desk, g_install, g_cancel, g_browse;
static HFONT g_font, g_bold;
static int g_choice;                           /* 0 = waiting, 1 = install, -1 = cancel */
static int g_dpi = 96;

#define UNINSTALL_KEY L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\PaintBooth"
#define ID_INSTALL 101
#define ID_CANCEL 102
#define ID_BROWSE 103
#define ID_DESK 104
#define SUBFOLDER L"Paintbooth"

/* Scale a 96-dpi pixel value to the screen's dpi. */
static int S(int v) { return MulDiv(v, g_dpi, 96); }

/* printf into a fixed buffer; returns 0 (and an empty string) if the result did not fit, so a
 * truncated path is never used. cap is in wchar_t and includes the terminating NUL. */
static int fmt(wchar_t *out, size_t cap, const wchar_t *f, ...)
{
    va_list ap;
    va_start(ap, f);
    int n = _vsnwprintf(out, cap, f, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= cap) { if (cap) out[0] = 0; return 0; }
    return 1;
}

/* Process pending window messages so the window stays responsive during long steps. */
static void pump(void)
{
    MSG m;
    while (PeekMessageW(&m, NULL, 0, 0, PM_REMOVE)) {
        if (!IsDialogMessageW(g_win, &m)) { TranslateMessage(&m); DispatchMessageW(&m); }
    }
}

/* Show a status line (options window or the small /S progress window). */
static void set_status(const wchar_t *text)
{
    if (g_label) { SetWindowTextW(g_label, text); pump(); }
}

/* Length of the root of a path that must not be created: "C:\", "\\server\share\" or
 * "\\?\C:\". Returns 0 for a relative path. */
static size_t root_len(const wchar_t *path)
{
    if (path[0] && path[1] == L':') return path[2] == L'\\' ? 3 : 2;
    if (path[0] == L'\\' && path[1] == L'\\') {
        const wchar_t *p = path + 2;
        if (p[0] == L'?' && p[1] == L'\\') {                      /* \\?\C:\... */
            p += 2;
            if (p[0] && p[1] == L':') return (size_t)(p - path) + (p[2] == L'\\' ? 3 : 2);
        }
        for (int seps = 0; *p; p++)                               /* \\server\share\ */
            if (*p == L'\\' && ++seps == 2) return (size_t)(p - path) + 1;
        return wcslen(path);
    }
    return 0;
}

/* Create every parent folder of path (not path itself). Works for drive and UNC paths. */
static void make_dirs(wchar_t *path)
{
    for (wchar_t *p = path + root_len(path); *p; p++) {
        if (*p == L'\\' && p > path && p[-1] != L'\\') { *p = 0; CreateDirectoryW(path, NULL); *p = L'\\'; }
    }
}

/* write_fn for Windows: create one payload entry below g_dir. Returns non-zero on failure. */
static int win_write(void *ctx, const char *name, const void *data, size_t size, int is_dir)
{
    (void)ctx;
    wchar_t rel[MAX_PATH * 2], full[MAX_PATH * 4];
    if (!safe_name(name)) return 1;                               /* also checked by extract_payload */
    int n = MultiByteToWideChar(CP_UTF8, 0, name, -1, rel, MAX_PATH * 2);
    if (n <= 0) return 1;
    for (wchar_t *p = rel; *p; p++) if (*p == L'/') *p = L'\\';
    if (!fmt(full, MAX_PATH * 4, L"%ls\\%ls", g_dir, rel)) return 1;
    make_dirs(full);
    if (is_dir) { CreateDirectoryW(full, NULL); return 0; }
    HANDLE h = CreateFileW(full, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return 1;
    const unsigned char *p = (const unsigned char *)data;
    size_t left = size;
    while (left) {                                                /* WriteFile takes a DWORD size */
        DWORD chunk = left > (1u << 30) ? (1u << 30) : (DWORD)left, wrote = 0;
        if (!WriteFile(h, p, chunk, &wrote, NULL) || wrote == 0) { CloseHandle(h); return 1; }
        p += wrote; left -= wrote;
    }
    CloseHandle(h);
    return 0;
}

/* progress_fn for Windows: "Installing Paintbooth...  42%". */
static void win_progress(void *ctx, unsigned done, unsigned total)
{
    (void)ctx;
    wchar_t t[128];
    fmt(t, 128, L"Installing Paintbooth...  %u%%", total ? done * 100 / total : 100);
    set_status(t);
}

/* Delete a folder and everything in it, silently (a missing folder is fine). */
static void remove_tree(const wchar_t *path)
{
    wchar_t from[DIR_CAP + 2];
    memset(from, 0, sizeof from);
    wcsncpy(from, path, DIR_CAP);              /* double-NUL terminated for SHFileOperation */
    SHFILEOPSTRUCTW op;
    memset(&op, 0, sizeof op);
    op.wFunc = FO_DELETE;
    op.pFrom = from;
    op.fFlags = FOF_NO_UI;
    SHFileOperationW(&op);
}

/* Run a command line without a console window and wait for it, keeping the UI alive.
 * Returns 0 if it could not be started; otherwise 1 and its exit code in *exit_code. */
static int run_and_wait(wchar_t *cmd, DWORD *exit_code)
{
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, g_dir, &si, &pi)) return 0;
    while (WaitForSingleObject(pi.hProcess, 50) == WAIT_TIMEOUT) pump();
    GetExitCodeProcess(pi.hProcess, exit_code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 1;
}

/* The folder of an earlier install (so updates go to the same place). cap is in wchar_t. */
static int previous_location(wchar_t *out, DWORD cap)
{
    HKEY k;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, UNINSTALL_KEY, 0, KEY_READ, &k) != ERROR_SUCCESS) return 0;
    DWORD type = 0, size = (cap - 1) * sizeof(wchar_t);           /* room for a NUL we add */
    LONG r = RegQueryValueExW(k, L"InstallLocation", NULL, &type, (BYTE *)out, &size);
    RegCloseKey(k);
    if (r != ERROR_SUCCESS || type != REG_SZ) { out[0] = 0; return 0; }
    out[size / sizeof(wchar_t)] = 0;                              /* registry strings need not be terminated */
    return out[0] != 0;
}

/* The program used to be called "Paint booth". An update of such an install keeps the parent folder
 * the user chose but uses the new name: "...\Paint booth" becomes "...\Paintbooth". (winsetup.py
 * then removes the program files from the old folder.) */
static void rename_old_folder(wchar_t *d)
{
    static const wchar_t old[] = L"Paint booth";
    size_t n = wcslen(d), k = wcslen(old);
    while (n > 3 && (d[n - 1] == L'\\' || d[n - 1] == L'/')) d[--n] = 0;
    if (n > k && d[n - k - 1] == L'\\' && _wcsicmp(d + n - k, old) == 0)
        wcscpy(d + n - k, SUBFOLDER);                             /* SUBFOLDER is shorter: it fits */
}

/* Trim trailing separators and make sure the folder ends in "\Paintbooth": a folder the user
 * picked gets its own subfolder. d must have room for DIR_CAP wchar_t. */
static void normalise_dir(wchar_t *d)
{
    size_t n = wcslen(d);
    while (n > 3 && (d[n - 1] == L'\\' || d[n - 1] == L'/')) d[--n] = 0;
    const wchar_t *tail = wcsrchr(d, L'\\');
    tail = tail ? tail + 1 : d;
    if (_wcsicmp(tail, SUBFOLDER) != 0 && n + 13 < DIR_CAP) wcscat(d, L"\\" SUBFOLDER);
}

/* Command-line options. */
typedef struct {
    int quiet;          /* /S */
    int desktop;        /* /DESKTOP */
    int have_dir;       /* /D=<folder> given */
    wchar_t dir[DIR_CAP];
} options;

/* Parse the installer's arguments (args is the command line without the program name).
 *
 * Switches are whole tokens (CommandLineToArgvW), so "/S" inside a folder name is not a switch.
 * /D=<folder> follows NSIS: it must be the last argument, may contain spaces, is not quoted,
 * and everything after "/D=" is the folder. It is only recognised at the start of a token
 * outside quotes. Returns 0 if memory or the folder length ran out. */
static int parse_args(const wchar_t *args, options *o)
{
    memset(o, 0, sizeof *o);
    if (!args) return 1;
    size_t len = wcslen(args), d = len;
    int quoted = 0;
    for (size_t i = 0; i < len; i++) {
        if (args[i] == L'"') quoted = !quoted;
        else if (!quoted && (i == 0 || args[i - 1] == L' ' || args[i - 1] == L'\t') && wcsncmp(args + i, L"/D=", 3) == 0) {
            d = i;
            break;
        }
    }
    if (d < len) {
        const wchar_t *v = args + d + 3;
        size_t vn = wcslen(v);
        while (vn && (v[vn - 1] == L' ' || v[vn - 1] == L'\t')) vn--;
        if (vn + 14 > DIR_CAP) return 0;                          /* + "\Paintbooth" + NUL */
        wmemcpy(o->dir, v, vn);
        o->dir[vn] = 0;
        o->have_dir = vn > 0;
    }
    wchar_t *head = (wchar_t *)malloc((d + 1) * sizeof(wchar_t));  /* the part before /D= */
    if (!head) return 0;
    wmemcpy(head, args, d);
    head[d] = 0;
    int ok = 1;
    if (head[0]) {
        int argc = 0;
        /* CommandLineToArgvW treats its first token as the program name, so prefix a dummy one */
        size_t hn = d + 4;
        wchar_t *line = (wchar_t *)malloc(hn * sizeof(wchar_t));
        if (!line) { free(head); return 0; }
        fmt(line, hn, L"x %ls", head);
        wchar_t **argv = CommandLineToArgvW(line, &argc);
        free(line);
        if (!argv) ok = 0;
        else {
            for (int i = 1; i < argc; i++) {
                if (wcscmp(argv[i], L"/S") == 0) o->quiet = 1;
                else if (wcscmp(argv[i], L"/DESKTOP") == 0) o->desktop = 1;
            }
            LocalFree(argv);
        }
    }
    free(head);
    return ok;
}

/* Browse dialog callback: preselect the current folder's parent. */
static int CALLBACK browse_cb(HWND h, UINT msg, LPARAM lp, LPARAM data)
{
    (void)lp;
    if (msg == BFFM_INITIALIZED) SendMessageW(h, BFFM_SETSELECTIONW, TRUE, data);
    return 0;
}

/* "Browse..." button: pick a parent folder; "\Paintbooth" is appended to it. */
static void browse(void)
{
    wchar_t cur[DIR_CAP], pick[DIR_CAP];                          /* pick: SHGetPathFromIDListW needs MAX_PATH */
    GetWindowTextW(g_edit, cur, DIR_CAP);
    BROWSEINFOW bi;
    ZeroMemory(&bi, sizeof bi);
    bi.hwndOwner = g_win;
    bi.lpszTitle = L"Choose where to install Paintbooth (a \"Paintbooth\" folder is created inside):";
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
    bi.lpfn = browse_cb;
    wchar_t parent[DIR_CAP];
    wcscpy(parent, cur);
    wchar_t *slash = wcsrchr(parent, L'\\');
    if (slash) *slash = 0;
    bi.lParam = (LPARAM)parent;
    PIDLIST_ABSOLUTE id = SHBrowseForFolderW(&bi);
    if (id) {
        if (SHGetPathFromIDListW(id, pick)) {
            normalise_dir(pick);
            SetWindowTextW(g_edit, pick);
        }
        CoTaskMemFree(id);
    }
}

/* Options window procedure: buttons set g_choice, which ask_options() waits for. */
static LRESULT CALLBACK wndproc(HWND h, UINT msg, WPARAM wp, LPARAM lp)
{
    switch (msg) {
    case WM_COMMAND:
        switch (LOWORD(wp)) {
        case ID_INSTALL: g_choice = 1; return 0;
        case ID_CANCEL: case IDCANCEL: g_choice = -1; return 0;
        case ID_BROWSE: browse(); return 0;
        }
        break;
    case WM_CTLCOLORSTATIC:
        SetBkMode((HDC)wp, TRANSPARENT);
        return (LRESULT)GetSysColorBrush(COLOR_WINDOW);
    case WM_CLOSE:
        if (g_choice == 0) g_choice = -1;
        return 0;
    }
    return DefWindowProcW(h, msg, wp, lp);
}

/* Create one child control at 96-dpi coordinates (scaled) with the given font. */
static HWND ctl(const wchar_t *cls, const wchar_t *text, DWORD style, int x, int y, int w, int hgt, int id, HFONT f)
{
    HWND c = CreateWindowExW(wcscmp(cls, L"EDIT") == 0 ? WS_EX_CLIENTEDGE : 0, cls, text, WS_CHILD | WS_VISIBLE | style,
                             S(x), S(y), S(w), S(hgt), g_win, (HMENU)(INT_PTR)id, GetModuleHandleW(NULL), NULL);
    SendMessageW(c, WM_SETFONT, (WPARAM)f, TRUE);
    return c;
}

/* Show the options window and wait for Install or Cancel.
 * Returns 1 for Install (g_dir and *desktop updated, controls disabled), 0 for Cancel. */
static int ask_options(HINSTANCE hInst, int *desktop)
{
    WNDCLASSW wc;
    ZeroMemory(&wc, sizeof wc);
    wc.lpfnWndProc = wndproc;
    wc.hInstance = hInst;
    wc.hCursor = LoadCursor(NULL, IDC_ARROW);
    wc.hbrBackground = GetSysColorBrush(COLOR_WINDOW);
    wc.hIcon = LoadIconW(hInst, MAKEINTRESOURCEW(1));
    wc.lpszClassName = L"PaintBoothSetup";
    RegisterClassW(&wc);
    int W = 520, H = 290;
    RECT r = {0, 0, S(W), S(H)};
    DWORD st = WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
    AdjustWindowRect(&r, st, FALSE);
    wchar_t title[128];
    fmt(title, 128, L"Paintbooth %ls Setup", SKY_VERSION);
    g_win = CreateWindowExW(0, wc.lpszClassName, title, st,
                            (GetSystemMetrics(SM_CXSCREEN) - (r.right - r.left)) / 2,
                            (GetSystemMetrics(SM_CYSCREEN) - (r.bottom - r.top)) / 2,
                            r.right - r.left, r.bottom - r.top, NULL, NULL, hInst, NULL);
    g_font = CreateFontW(-S(12), 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, L"Segoe UI");
    g_bold = CreateFontW(-S(20), 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, L"Segoe UI");
    wchar_t head[128];
    fmt(head, 128, L"Paintbooth %ls", SKY_VERSION);
    ctl(L"STATIC", head, 0, 20, 16, 480, 30, 0, g_bold);
    ctl(L"STATIC", L"Skin mods for Euro Truck Simulator 2 and American Truck Simulator.\n"
                   L"By AL3X \x00b7 MIT licence \x00b7 github.com/AL3X-7777/paintbooth", 0, 20, 50, 480, 36, 0, g_font);
    ctl(L"STATIC", L"Install to:", 0, 20, 100, 480, 18, 0, g_font);
    g_edit = ctl(L"EDIT", g_dir, ES_AUTOHSCROLL | WS_TABSTOP, 20, 120, 380, 24, 0, g_font);
    g_browse = ctl(L"BUTTON", L"Browse...", BS_PUSHBUTTON | WS_TABSTOP, 408, 119, 92, 26, ID_BROWSE, g_font);
    g_desk = ctl(L"BUTTON", L"Create a desktop icon", BS_AUTOCHECKBOX | WS_TABSTOP, 20, 156, 300, 22, ID_DESK, g_font);
    ctl(L"STATIC", L"A Start menu entry is always added. No admin rights needed. "
                   L"Your projects stay in Documents\\Paintbooth. Close Paintbooth before updating.",
        0, 20, 186, 480, 36, 0, g_font);
    g_label = ctl(L"STATIC", L"", 0, 20, 252, 270, 20, 0, g_font);
    g_install = ctl(L"BUTTON", L"Install", BS_DEFPUSHBUTTON | WS_TABSTOP, 300, 246, 96, 30, ID_INSTALL, g_font);
    g_cancel = ctl(L"BUTTON", L"Cancel", BS_PUSHBUTTON | WS_TABSTOP, 404, 246, 96, 30, ID_CANCEL, g_font);
    ShowWindow(g_win, SW_SHOW);
    SetForegroundWindow(g_win);
    SetFocus(g_install);
    while (g_choice == 0) {
        MSG m;
        if (GetMessageW(&m, NULL, 0, 0) <= 0) { g_choice = -1; break; }
        if (!IsDialogMessageW(g_win, &m)) { TranslateMessage(&m); DispatchMessageW(&m); }
    }
    if (g_choice < 0) { DestroyWindow(g_win); g_win = NULL; return 0; }
    GetWindowTextW(g_edit, g_dir, DIR_CAP - 14);                  /* leave room for "\Paintbooth" */
    normalise_dir(g_dir);
    SetWindowTextW(g_edit, g_dir);
    *desktop = SendMessageW(g_desk, BM_GETCHECK, 0, 0) == BST_CHECKED;
    EnableWindow(g_edit, FALSE); EnableWindow(g_browse, FALSE); EnableWindow(g_desk, FALSE);
    EnableWindow(g_install, FALSE); EnableWindow(g_cancel, FALSE);
    return 1;
}

/* Show an error box and return 1 (the installer's exit code for a failure). */
static int fail(const wchar_t *text)
{
    MessageBoxW(g_win, text, L"Paintbooth Setup", MB_ICONERROR);
    if (g_win) DestroyWindow(g_win);
    return 1;
}

/* Read the whole installer file into a malloc'ed buffer. Returns NULL on any failure. */
static unsigned char *read_self(size_t *out_size)
{
    wchar_t self[DIR_CAP];
    DWORD sn = GetModuleFileNameW(NULL, self, DIR_CAP);
    if (sn == 0 || sn >= DIR_CAP) return NULL;
    HANDLE h = CreateFileW(self, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return NULL;
    LARGE_INTEGER sz;
    if (!GetFileSizeEx(h, &sz) || sz.QuadPart <= 0) { CloseHandle(h); return NULL; }
    size_t want_total = (size_t)sz.QuadPart;
    unsigned char *buf = (unsigned char *)malloc(want_total);
    if (!buf) { CloseHandle(h); return NULL; }
    size_t total = 0;
    while (total < want_total) {                                  /* ReadFile takes a DWORD size */
        DWORD got = 0, want = (DWORD)((want_total - total) > (1u << 30) ? (1u << 30) : (want_total - total));
        if (!ReadFile(h, buf + total, want, &got, NULL) || got == 0) break;
        total += got;
    }
    CloseHandle(h);
    if (total != want_total) { free(buf); return NULL; }
    *out_size = total;
    return buf;
}

/* Small borderless "Installing..." window used with /S. */
static void quiet_window(HINSTANCE hInst)
{
    g_win = CreateWindowExW(WS_EX_TOOLWINDOW, L"STATIC", L"Paintbooth Setup", WS_POPUP | WS_BORDER | WS_VISIBLE,
                            (GetSystemMetrics(SM_CXSCREEN) - 420) / 2, (GetSystemMetrics(SM_CYSCREEN) - 90) / 2,
                            420, 90, NULL, NULL, hInst, NULL);
    g_label = CreateWindowExW(0, L"STATIC", L"Preparing...", WS_CHILD | WS_VISIBLE | SS_CENTER | SS_CENTERIMAGE,
                              0, 0, 418, 88, g_win, NULL, hInst, NULL);
    SendMessageW(g_label, WM_SETFONT, (WPARAM)GetStockObject(DEFAULT_GUI_FONT), TRUE);
}

/* Entry point: see "Install steps" at the top of this file. Returns 0 on success or cancel. */
int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, PWSTR args, int show)
{
    (void)hPrev; (void)show;
    SetProcessDPIAware();
    HDC sdc = GetDC(NULL);
    g_dpi = GetDeviceCaps(sdc, LOGPIXELSX);
    ReleaseDC(NULL, sdc);
    CoInitialize(NULL);

    /* 1-2: options and install folder */
    static options opt;
    if (!parse_args(args, &opt)) return fail(L"The install folder given with /D= is too long.");
    int desktop = opt.desktop;
    wchar_t local[MAX_PATH];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, local))) return 1;
    if (previous_location(g_dir, DIR_CAP - 14)) rename_old_folder(g_dir);
    else if (!fmt(g_dir, DIR_CAP, L"%ls\\Programs\\" SUBFOLDER, local)) return 1;
    if (opt.have_dir) { wcscpy(g_dir, opt.dir); rename_old_folder(g_dir); normalise_dir(g_dir); }

    /* 3: ask, or show the small progress window */
    if (!opt.quiet) {
        if (!ask_options(hInst, &desktop)) return 0;
    } else {
        quiet_window(hInst);
    }
    set_status(L"Preparing...");

    /* 4: read ourselves (unicode-safe path), remove the old program files, unpack */
    size_t total = 0;
    unsigned char *buf = read_self(&total);
    if (!buf) return fail(L"Could not read the installer file.");

    wchar_t old[DIR_CAP + 16];
    set_status(L"Removing the previous version...");
    if (fmt(old, DIR_CAP + 16, L"%ls\\runtime", g_dir)) remove_tree(old);
    if (fmt(old, DIR_CAP + 16, L"%ls\\app", g_dir)) remove_tree(old);
    if (fmt(old, DIR_CAP + 16, L"%ls\\x", g_dir)) make_dirs(old);  /* creates g_dir and its parents */

    int rc = extract_payload(buf, total, win_write, win_progress, NULL);
    free(buf);
    if (rc) {
        wchar_t m[MAX_PATH * 3];
        fmt(m, MAX_PATH * 3, L"Installing failed (code %d).\n\nCan Paintbooth write to\n%ls ?\n\n"
                             L"If Paintbooth is running, close it and run the installer again.", rc, g_dir);
        return fail(m);
    }

    /* 5: shortcuts, uninstall entry, file types (winsetup.py) */
    set_status(L"Creating shortcuts...");
    wchar_t cmd[MAX_PATH * 6];
    DWORD code = 1;
    int ran = fmt(cmd, MAX_PATH * 6, L"\"%ls\\runtime\\pythonw.exe\" \"%ls\\app\\winsetup.py\" install%ls", g_dir, g_dir,
                  desktop ? L" --desktop" : L"") && run_and_wait(cmd, &code);
    if (!ran || code != 0) {
        wchar_t m[MAX_PATH * 3];
        fmt(m, MAX_PATH * 3, L"Paintbooth was copied, but creating the shortcuts failed.\n"
                             L"You can start it from %ls\\Paintbooth.exe.", g_dir);
        MessageBoxW(g_win, m, L"Paintbooth Setup", MB_ICONWARNING);
    }

    /* 6: done */
    set_status(L"Done.");
    if (!opt.quiet && MessageBoxW(g_win, L"Paintbooth is installed.\n\nStart it now?", L"Paintbooth Setup",
                                  MB_YESNO | MB_ICONINFORMATION) == IDYES) {
        wchar_t exe[DIR_CAP + 32];
        if (fmt(exe, DIR_CAP + 32, L"%ls\\Paintbooth.exe", g_dir)) ShellExecuteW(NULL, L"open", exe, NULL, g_dir, SW_SHOWNORMAL);
    }
    if (g_win) DestroyWindow(g_win);
    return 0;
}
#endif
