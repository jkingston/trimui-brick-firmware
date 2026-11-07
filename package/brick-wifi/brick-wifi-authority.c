// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_CLIENTS 8
#define MAX_NETWORKS 32
#define MAX_PACKET 16384
#define MAX_SSID 96

enum security { SECURITY_OPEN, SECURITY_WPA2, SECURITY_UNSUPPORTED };

struct network {
    char identity[65];
    unsigned char ssid[MAX_SSID];
    size_t ssid_len;
    enum security security;
    int saved_id;
    int signal;
    bool in_range;
    bool current;
    bool auto_connect;
    bool private_address;
    bool auth_failed;
};

struct client {
    int fd;
    uint32_t last_request;
    char last_result[384];
};

static volatile sig_atomic_t stopping;
static uint32_t generation;
static uint32_t revision;
static bool radio_failed;
static struct network networks[MAX_NETWORKS];
static size_t network_count;
static char current_state[32];
static char current_address[64];
static char current_bssid[32];

static const char *env_or(const char *name, const char *fallback)
{
    const char *value = getenv(name);
    return value && *value ? value : fallback;
}

static void stop_handler(int signal_number)
{
    (void)signal_number;
    stopping = 1;
}

static bool read_first_line(const char *path, char *out, size_t size)
{
    FILE *file = fopen(path, "re");
    if (!file)
        return false;
    bool ok = fgets(out, (int)size, file) != NULL;
    fclose(file);
    if (!ok)
        return false;
    out[strcspn(out, "\r\n")] = '\0';
    return true;
}

static bool write_atomic(const char *path, const char *value)
{
    char temporary[512];
    if (snprintf(temporary, sizeof(temporary), "%s.new.%ld", path,
                 (long)getpid()) >= (int)sizeof(temporary))
        return false;
    int fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0)
        return false;
    size_t length = strlen(value);
    bool ok = write(fd, value, length) == (ssize_t)length && fsync(fd) == 0;
    if (close(fd) < 0)
        ok = false;
    if (ok)
        ok = rename(temporary, path) == 0;
    if (!ok)
        unlink(temporary);
    sync();
    return ok;
}

static int run_program(const char *path, const char *first, const char *second)
{
    pid_t child = fork();
    if (child < 0)
        return -1;
    if (child == 0) {
        if (second)
            execl(path, path, first, second, (char *)NULL);
        else if (first)
            execl(path, path, first, (char *)NULL);
        else
            execl(path, path, (char *)NULL);
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0)
        if (errno != EINTR)
            return -1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static bool run_detached(const char *path, const char *argument)
{
    pid_t child = fork();
    if (child < 0)
        return false;
    if (child == 0) {
        pid_t grandchild = fork();
        if (grandchild < 0)
            _exit(127);
        if (grandchild == 0) {
            setsid();
            execl(path, path, argument, (char *)NULL);
            _exit(127);
        }
        _exit(0);
    }
    int status;
    while (waitpid(child, &status, 0) < 0)
        if (errno != EINTR)
            return false;
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

static int hex_value(char value)
{
    if (value >= '0' && value <= '9')
        return value - '0';
    if (value >= 'a' && value <= 'f')
        return value - 'a' + 10;
    if (value >= 'A' && value <= 'F')
        return value - 'A' + 10;
    return -1;
}

static size_t decode_wpa_text(const char *input, unsigned char *output,
                              size_t capacity)
{
    size_t used = 0;
    while (*input && used < capacity) {
        if (*input != '\\') {
            output[used++] = (unsigned char)*input++;
            continue;
        }
        input++;
        if (!*input)
            break;
        if (*input == 'x' && hex_value(input[1]) >= 0 && hex_value(input[2]) >= 0) {
            output[used++] = (unsigned char)((hex_value(input[1]) << 4) |
                                              hex_value(input[2]));
            input += 3;
        } else {
            switch (*input) {
            case 'n': output[used++] = '\n'; break;
            case 'r': output[used++] = '\r'; break;
            case 't': output[used++] = '\t'; break;
            case 'e': output[used++] = 27; break;
            default: output[used++] = (unsigned char)*input; break;
            }
            input++;
        }
    }
    return used;
}

static bool decode_hex(const char *input, unsigned char *output, size_t capacity,
                       size_t *length)
{
    size_t size = strlen(input);
    if ((size & 1) || size / 2 > capacity)
        return false;
    for (size_t index = 0; index < size / 2; index++) {
        int high = hex_value(input[index * 2]);
        int low = hex_value(input[index * 2 + 1]);
        if (high < 0 || low < 0)
            return false;
        output[index] = (unsigned char)((high << 4) | low);
    }
    *length = size / 2;
    return true;
}

static void append_hex(char *output, size_t capacity, size_t *used,
                       const unsigned char *input, size_t length)
{
    static const char digits[] = "0123456789abcdef";
    for (size_t index = 0; index < length && *used + 2 < capacity; index++) {
        output[(*used)++] = digits[input[index] >> 4];
        output[(*used)++] = digits[input[index] & 15];
    }
    output[*used] = '\0';
}

static uint64_t identity_hash(const unsigned char *ssid, size_t length,
                              enum security security)
{
    uint64_t hash = UINT64_C(1469598103934665603);
    for (size_t index = 0; index < length; index++) {
        hash ^= ssid[index];
        hash *= UINT64_C(1099511628211);
    }
    hash ^= (unsigned int)security;
    return hash * UINT64_C(1099511628211);
}

static int wpa_command(const char *command, char *response, size_t capacity)
{
    const char *ctrl = env_or("WIFI_CTRL_DIR", "/run/wpa_supplicant");
    const char *state = env_or("WIFI_STATE_DIR", "/run/brick-wifi");
    char remote_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    char local_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    static unsigned int sequence;
    if (snprintf(remote_path, sizeof(remote_path), "%s/wlan0", ctrl) >=
        (int)sizeof(remote_path) ||
        snprintf(local_path, sizeof(local_path), "%s/wpa-%ld-%u", state,
                 (long)getpid(), ++sequence) >= (int)sizeof(local_path))
        return -1;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    struct sockaddr_un local = { .sun_family = AF_UNIX };
    struct sockaddr_un remote = { .sun_family = AF_UNIX };
    memcpy(local.sun_path, local_path, strlen(local_path) + 1);
    memcpy(remote.sun_path, remote_path, strlen(remote_path) + 1);
    unlink(local_path);
    int result = -1;
    if (bind(fd, (struct sockaddr *)&local,
             offsetof(struct sockaddr_un, sun_path) + strlen(local_path) + 1) < 0 ||
        connect(fd, (struct sockaddr *)&remote,
                offsetof(struct sockaddr_un, sun_path) + strlen(remote_path) + 1) < 0)
        goto out;
    size_t length = strlen(command);
    if (send(fd, command, length, MSG_NOSIGNAL) != (ssize_t)length)
        goto out;
    struct pollfd wait = { .fd = fd, .events = POLLIN };
    int ready;
    do ready = poll(&wait, 1, 3000); while (ready < 0 && errno == EINTR);
    if (ready != 1)
        goto out;
    ssize_t received = recv(fd, response, capacity - 1, 0);
    if (received < 0)
        goto out;
    response[received] = '\0';
    result = 0;
out:
    close(fd);
    unlink(local_path);
    return result;
}

static bool wpa_ok(const char *format, ...)
{
    char command[512], response[128];
    va_list arguments;
    va_start(arguments, format);
    int length = vsnprintf(command, sizeof(command), format, arguments);
    va_end(arguments);
    bool ok = length > 0 && length < (int)sizeof(command) &&
              wpa_command(command, response, sizeof(response)) == 0 &&
              strcmp(response, "OK\n") == 0;
    explicit_bzero(command, sizeof(command));
    explicit_bzero(response, sizeof(response));
    return ok;
}

static enum security scan_security(const char *flags)
{
    /* XR829 appends [SAE-H2E] as an AP capability even when the advertised
     * suite is WPA2-PSK.  Classify the suite token, not that capability. */
    if (strstr(flags, "[WPA2-PSK-CCMP]") && !strstr(flags, "TKIP") &&
        !strstr(flags, "WEP"))
        return SECURITY_WPA2;
    if (strstr(flags, "SAE") || strstr(flags, "TKIP") || strstr(flags, "WEP"))
        return SECURITY_UNSUPPORTED;
    if (!strstr(flags, "WPA") && !strstr(flags, "RSN"))
        return SECURITY_OPEN;
    return SECURITY_UNSUPPORTED;
}

static const char *security_name(enum security security)
{
    switch (security) {
    case SECURITY_OPEN: return "open";
    case SECURITY_WPA2: return "wpa2_personal";
    default: return "unsupported";
    }
}

static int split_tabs(char *line, char **fields, int maximum)
{
    int count = 0;
    while (count < maximum) {
        fields[count++] = line;
        char *tab = strchr(line, '\t');
        if (!tab)
            break;
        *tab = '\0';
        line = tab + 1;
    }
    return count;
}

static bool same_network(const struct network *network,
                         const unsigned char *ssid, size_t length,
                         enum security security)
{
    return network->ssid_len == length && network->security == security &&
           memcmp(network->ssid, ssid, length) == 0;
}

static enum security saved_security(int id)
{
    char command[64], response[256];
    snprintf(command, sizeof(command), "GET_NETWORK %d key_mgmt", id);
    if (wpa_command(command, response, sizeof(response)) < 0)
        return SECURITY_UNSUPPORTED;
    response[strcspn(response, "\r\n")] = '\0';
    if (!strcmp(response, "NONE"))
        return SECURITY_OPEN;
    /* Stock-created profiles contain the downstream default token list
     * "WPA-PSK WPA-EAP".  Presence of WPA-PSK is the relevant capability;
     * product-created profiles are narrowed to WPA2/CCMP below. */
    if (strstr(response, "WPA-PSK") || strstr(response, "WPA2-PSK"))
        return SECURITY_WPA2;
    return SECURITY_UNSUPPORTED;
}

static bool network_value(int id, const char *field, char *value, size_t size)
{
    char command[96];
    snprintf(command, sizeof(command), "GET_NETWORK %d %s", id, field);
    if (wpa_command(command, value, size) < 0)
        return false;
    value[strcspn(value, "\r\n")] = '\0';
    return strcmp(value, "FAIL") != 0;
}

static void read_status(int *current_id)
{
    char response[4096];
    *current_id = -1;
    strcpy(current_state, "DISCONNECTED");
    current_address[0] = '\0';
    current_bssid[0] = '\0';
    if (wpa_command("STATUS", response, sizeof(response)) < 0)
        return;
    char *save = NULL;
    for (char *line = strtok_r(response, "\n", &save); line;
         line = strtok_r(NULL, "\n", &save)) {
        if (!strncmp(line, "id=", 3))
            *current_id = atoi(line + 3);
        else if (!strncmp(line, "wpa_state=", 10))
            snprintf(current_state, sizeof(current_state), "%s", line + 10);
        else if (!strncmp(line, "ip_address=", 11))
            snprintf(current_address, sizeof(current_address), "%s", line + 11);
        else if (!strncmp(line, "bssid=", 6))
            snprintf(current_bssid, sizeof(current_bssid), "%s", line + 6);
    }
}

static void load_networks(void)
{
    network_count = 0;
    int current_id;
    read_status(&current_id);
    char response[MAX_PACKET];
    if (wpa_command("LIST_NETWORKS", response, sizeof(response)) == 0) {
        char *save = NULL;
        bool header = true;
        for (char *line = strtok_r(response, "\n", &save); line;
             line = strtok_r(NULL, "\n", &save)) {
            if (header) { header = false; continue; }
            if (network_count >= MAX_NETWORKS)
                break;
            char *fields[4];
            int fields_count = split_tabs(line, fields, 4);
            if (fields_count < 2)
                continue;
            char *end;
            long id = strtol(fields[0], &end, 10);
            if (*fields[0] == '\0' || *end || id < 0 || id > INT32_MAX)
                continue;
            struct network *network = &networks[network_count++];
            memset(network, 0, sizeof(*network));
            network->saved_id = (int)id;
            snprintf(network->identity, sizeof(network->identity), "n%d", network->saved_id);
            network->ssid_len = decode_wpa_text(fields[1], network->ssid,
                                                sizeof(network->ssid));
            network->security = saved_security(network->saved_id);
            network->signal = -100;
            network->current = network->saved_id == current_id;
            char value[128];
            network->auto_connect = network_value(network->saved_id, "disabled", value,
                                                  sizeof(value)) && strcmp(value, "1");
            network->private_address = network_value(network->saved_id, "mac_addr", value,
                                                     sizeof(value)) && !strcmp(value, "3");
            network->auth_failed = fields_count >= 4 &&
                                   strstr(fields[3], "TEMP-DISABLED") != NULL;
        }
    }
    if (wpa_command("SCAN_RESULTS", response, sizeof(response)) == 0) {
        char *save = NULL;
        bool header = true;
        for (char *line = strtok_r(response, "\n", &save); line;
             line = strtok_r(NULL, "\n", &save)) {
            if (header) { header = false; continue; }
            char *fields[5];
            if (split_tabs(line, fields, 5) < 5)
                continue;
            unsigned char ssid[MAX_SSID];
            size_t ssid_len = decode_wpa_text(fields[4], ssid, sizeof(ssid));
            if (!ssid_len)
                continue;
            bool hidden = true;
            for (size_t index = 0; index < ssid_len; index++)
                if (ssid[index] != 0) { hidden = false; break; }
            if (hidden)
                continue;
            enum security security = scan_security(fields[3]);
            int signal = atoi(fields[2]);
            struct network *match = NULL;
            for (size_t index = 0; index < network_count; index++)
                if (same_network(&networks[index], ssid, ssid_len, security)) {
                    match = &networks[index];
                    break;
                }
            if (!match) {
                if (network_count >= MAX_NETWORKS)
                    continue;
                match = &networks[network_count++];
                memset(match, 0, sizeof(*match));
                memcpy(match->ssid, ssid, ssid_len);
                match->ssid_len = ssid_len;
                match->security = security;
                match->saved_id = -1;
                snprintf(match->identity, sizeof(match->identity), "s%016llx",
                         (unsigned long long)identity_hash(ssid, ssid_len, security));
                match->signal = signal;
            }
            match->in_range = true;
            if (signal > match->signal)
                match->signal = signal;
        }
    }
}

static int network_rank(const struct network *network)
{
    if (network->current) return 0;
    if (network->saved_id >= 0 && network->in_range) return 1;
    if (network->saved_id < 0) return 2;
    return 3;
}

static int compare_networks(const void *left, const void *right)
{
    const struct network *a = left, *b = right;
    int rank = network_rank(a) - network_rank(b);
    if (rank) return rank;
    if (a->signal != b->signal) return b->signal - a->signal;
    size_t minimum = a->ssid_len < b->ssid_len ? a->ssid_len : b->ssid_len;
    int name = memcmp(a->ssid, b->ssid, minimum);
    return name ? name : (int)a->ssid_len - (int)b->ssid_len;
}

static const char *connectivity_state(void)
{
    static char state[32];
    char path[512], line[256];
    snprintf(path, sizeof(path), "%s/connectivity",
             env_or("WIFI_STATE_DIR", "/run/brick-wifi"));
    FILE *file = fopen(path, "re");
    if (!file)
        return "none";
    strcpy(state, "none");
    while (fgets(line, sizeof(line), file))
        if (!strncmp(line, "state=", 6)) {
            line[strcspn(line, "\r\n")] = '\0';
            if (!strcmp(line + 6, "online")) strcpy(state, "internet");
            else if (!strcmp(line + 6, "captive")) strcpy(state, "captive");
            else if (!strcmp(line + 6, "limited")) strcpy(state, "limited");
            break;
        }
    fclose(file);
    return state;
}

static const char *network_state(const struct network *network,
                                 const char *connectivity)
{
    if (network->security == SECURITY_UNSUPPORTED)
        return "unsupported";
    if (!network->current)
        return network->auth_failed ? "auth_failed" :
            network->saved_id >= 0 ? (network->in_range ? "saved" : "out_of_range") : "available";
    if (!strcmp(current_state, "COMPLETED")) {
        if (!strcmp(connectivity, "internet")) return "connected";
        if (!strcmp(connectivity, "captive")) return "sign_in";
        if (!strcmp(connectivity, "limited")) return "limited";
        return "validating";
    }
    if (!strcmp(current_state, "SCANNING") || !strcmp(current_state, "DISCONNECTED"))
        return "reconnecting";
    return "connecting";
}

static const char *signal_name(int signal)
{
    if (signal >= -55) return "strong";
    if (signal >= -67) return "good";
    if (signal >= -75) return "fair";
    return "weak";
}

static bool wifi_enabled(void)
{
    char value[16], path[512];
    snprintf(path, sizeof(path), "%s/enabled",
             env_or("WIFI_DATA_DIR", "/data/wifi"));
    return read_first_line(path, value, sizeof(value)) && !strcmp(value, "1");
}

static const char *radio_state(void)
{
    if (!wifi_enabled()) return "off";
    if (radio_failed) return "failed";
    char path[512];
    snprintf(path, sizeof(path), "%s/enabled",
             env_or("WIFI_STATE_DIR", "/run/brick-wifi"));
    return access(path, F_OK) == 0 ? "ready" : "starting";
}

static size_t build_snapshot(char *packet, size_t capacity, uint32_t packet_revision)
{
    load_networks();
    qsort(networks, network_count, sizeof(networks[0]), compare_networks);
    const char *radio = radio_state();
    const char *connectivity = !strcmp(radio, "ready") ? connectivity_state() : "none";
    size_t used = (size_t)snprintf(packet, capacity,
        "snapshot generation=%u revision=%u radio=%s connectivity=%s can_scan=%d\n",
        generation, packet_revision, radio, connectivity, !strcmp(radio, "ready"));
    for (size_t index = 0; index < network_count && used < capacity; index++) {
        struct network *network = &networks[index];
        int length = snprintf(packet + used, capacity - used,
            "network id=%s name_hex=", network->identity);
        if (length < 0 || (size_t)length >= capacity - used) break;
        used += (size_t)length;
        append_hex(packet, capacity, &used, network->ssid, network->ssid_len);
        length = snprintf(packet + used, capacity - used,
            " security=%s signal=%s state=%s saved=%d current=%d in_range=%d auto=%d private=%d address_hex=",
            security_name(network->security), signal_name(network->signal),
            network_state(network, connectivity), network->saved_id >= 0,
            network->current, network->in_range, network->auto_connect,
            network->private_address);
        if (length < 0 || (size_t)length >= capacity - used) break;
        used += (size_t)length;
        if (network->current)
            append_hex(packet, capacity, &used,
                       (const unsigned char *)current_address, strlen(current_address));
        if (used + 1 >= capacity) break;
        packet[used++] = '\n'; packet[used] = '\0';
    }
    if (used + 4 < capacity) {
        memcpy(packet + used, "end\n", 5);
        used += 4;
    }
    return used;
}

static struct network *find_network(const char *identity)
{
    for (size_t index = 0; index < network_count; index++)
        if (!strcmp(networks[index].identity, identity))
            return &networks[index];
    return NULL;
}

static const char *field_value(const char *packet, const char *key,
                               char *output, size_t size)
{
    size_t key_length = strlen(key);
    const char *cursor = packet;
    while ((cursor = strstr(cursor, key))) {
        if ((cursor == packet || cursor[-1] == ' ') && cursor[key_length] == '=') {
            cursor += key_length + 1;
            size_t length = strcspn(cursor, " \n");
            if (length >= size)
                return NULL;
            memcpy(output, cursor, length);
            output[length] = '\0';
            return output;
        }
        cursor += key_length;
    }
    return NULL;
}

static bool valid_identity(const char *identity)
{
    if (!*identity || strlen(identity) >= 65)
        return false;
    for (const char *cursor = identity; *cursor; cursor++)
        if (!isalnum((unsigned char)*cursor) && *cursor != '-' && *cursor != '_')
            return false;
    return true;
}

static bool quote_value(const unsigned char *value, size_t length,
                        char *output, size_t capacity)
{
    size_t used = 0;
    if (capacity < 3) return false;
    output[used++] = '"';
    for (size_t index = 0; index < length; index++) {
        if (value[index] < 32 || value[index] > 126)
            return false;
        if (value[index] == '"' || value[index] == '\\') {
            if (used + 2 >= capacity) return false;
            output[used++] = '\\';
        }
        if (used + 1 >= capacity) return false;
        output[used++] = (char)value[index];
    }
    output[used++] = '"';
    output[used] = '\0';
    return true;
}

static bool configure_new_network(const unsigned char *ssid, size_t ssid_len,
                                  enum security security,
                                  const unsigned char *credential,
                                  size_t credential_len, bool hidden,
                                  int *network_id)
{
    char response[128];
    if (wpa_command("ADD_NETWORK", response, sizeof(response)) < 0)
        return false;
    char *end;
    long id = strtol(response, &end, 10);
    if (id < 0 || id > INT32_MAX || (*end != '\n' && *end != '\0'))
        return false;
    *network_id = (int)id;
    char ssid_hex[MAX_SSID * 2 + 1] = {0};
    size_t used = 0;
    append_hex(ssid_hex, sizeof(ssid_hex), &used, ssid, ssid_len);
    bool ok = wpa_ok("SET_NETWORK %d ssid %s", *network_id, ssid_hex);
    if (security == SECURITY_OPEN)
        ok = ok && wpa_ok("SET_NETWORK %d key_mgmt NONE", *network_id);
    else if (security == SECURITY_WPA2 && credential_len >= 8 && credential_len <= 63) {
        char quoted[256];
        ok = ok && quote_value(credential, credential_len, quoted, sizeof(quoted)) &&
             wpa_ok("SET_NETWORK %d key_mgmt WPA-PSK", *network_id) &&
             wpa_ok("SET_NETWORK %d proto RSN", *network_id) &&
             wpa_ok("SET_NETWORK %d pairwise CCMP", *network_id) &&
             wpa_ok("SET_NETWORK %d group CCMP", *network_id) &&
             wpa_ok("SET_NETWORK %d psk %s", *network_id, quoted);
        memset(quoted, 0, sizeof(quoted));
    } else ok = false;
    if (hidden)
        ok = ok && wpa_ok("SET_NETWORK %d scan_ssid 1", *network_id);
    if (ok) {
        char id_text[32];
        snprintf(id_text, sizeof(id_text), "%d", *network_id);
        ok = run_program(env_or("WIFI_PRIVATE_MAC", "/usr/libexec/brick-wifi-private-mac"),
                         "prepare", id_text) == 0;
    }
    if (!ok)
        wpa_ok("REMOVE_NETWORK %d", *network_id);
    return ok;
}

static bool select_network(int id)
{
    return wpa_ok("ENABLE_NETWORK %d", id) &&
           wpa_ok("SELECT_NETWORK %d", id) &&
           wpa_ok("ENABLE_NETWORK all") && wpa_ok("SAVE_CONFIG");
}

static bool wait_disconnected(void)
{
    for (int attempt = 0; attempt < 20; attempt++) {
        int id;
        read_status(&id);
        if (strcmp(current_state, "COMPLETED"))
            return true;
        struct timespec delay = { .tv_nsec = 100000000 };
        nanosleep(&delay, NULL);
    }
    return false;
}

static bool set_enabled(bool enabled)
{
    char path[512];
    snprintf(path, sizeof(path), "%s/enabled",
             env_or("WIFI_DATA_DIR", "/data/wifi"));
    if (!write_atomic(path, enabled ? "1\n" : "0\n"))
        return false;
    int result = run_program(env_or("WIFI_SERVICE", "/etc/init.d/S45wifi"),
                             enabled ? "force-start" : "stop", NULL);
    radio_failed = result != 0;
    return result == 0;
}

static bool execute_command(const char *packet, char *reason, size_t reason_size)
{
    char kind[64], identity[65], value[8], security_text[32];
    char ssid_hex[MAX_SSID * 2 + 1], credential_hex[MAX_SSID * 2 + 1];
    bool have_identity = field_value(packet, "network", identity, sizeof(identity));
    if (!field_value(packet, "kind", kind, sizeof(kind)) ||
        !field_value(packet, "value", value, sizeof(value)) ||
        !field_value(packet, "security", security_text, sizeof(security_text)) ||
        !field_value(packet, "ssid_hex", ssid_hex, sizeof(ssid_hex)) ||
        !field_value(packet, "credential_hex", credential_hex, sizeof(credential_hex))) {
        snprintf(reason, reason_size, "invalid-command");
        return false;
    }
    if (have_identity && !valid_identity(identity)) {
        snprintf(reason, reason_size, "invalid-network");
        return false;
    }
    load_networks();
    struct network *network = have_identity ? find_network(identity) : NULL;
    if (!strcmp(kind, "set_enabled"))
        return set_enabled(!strcmp(value, "1"));
    if (!wifi_enabled()) {
        snprintf(reason, reason_size, "wifi-off");
        return false;
    }
    if (!strcmp(kind, "scan"))
        return wpa_ok("SCAN");
    if (!strcmp(kind, "cancel") || !strcmp(kind, "disconnect"))
        return wpa_ok("DISCONNECT");
    if (!strcmp(kind, "connect")) {
        unsigned char ssid[MAX_SSID], credential[MAX_SSID];
        size_t ssid_len = 0, credential_len = 0;
        enum security security = !strcmp(security_text, "open") ? SECURITY_OPEN :
            !strcmp(security_text, "wpa2_personal") ? SECURITY_WPA2 : SECURITY_UNSUPPORTED;
        bool hidden = !network;
        if (!decode_hex(credential_hex, credential, sizeof(credential), &credential_len)) {
            snprintf(reason, reason_size, "invalid-credential");
            return false;
        }
        if (network && network->saved_id >= 0) {
            bool ok = true;
            if (credential_len) {
                char quoted[256];
                ok = network->security == SECURITY_WPA2 && credential_len >= 8 &&
                     credential_len <= 63 &&
                     quote_value(credential, credential_len, quoted, sizeof(quoted)) &&
                     wpa_ok("SET_NETWORK %d psk %s", network->saved_id, quoted) &&
                     wpa_ok("SAVE_CONFIG");
                explicit_bzero(quoted, sizeof(quoted));
            }
            if (network->security == SECURITY_WPA2)
                ok = ok && wpa_ok("SET_NETWORK %d key_mgmt WPA-PSK", network->saved_id) &&
                     wpa_ok("SET_NETWORK %d proto RSN", network->saved_id) &&
                     wpa_ok("SET_NETWORK %d pairwise CCMP", network->saved_id) &&
                     wpa_ok("SET_NETWORK %d group CCMP", network->saved_id);
            else if (network->security == SECURITY_OPEN)
                ok = ok && wpa_ok("SET_NETWORK %d key_mgmt NONE", network->saved_id);
            explicit_bzero(credential, sizeof(credential));
            return ok && select_network(network->saved_id);
        }
        if (network) {
            memcpy(ssid, network->ssid, network->ssid_len);
            ssid_len = network->ssid_len;
            security = network->security;
        } else if (!decode_hex(ssid_hex, ssid, sizeof(ssid), &ssid_len) || !ssid_len) {
            snprintf(reason, reason_size, "invalid-ssid");
            return false;
        }
        int id = -1;
        bool ok = configure_new_network(ssid, ssid_len, security, credential,
                                        credential_len, hidden, &id) && select_network(id);
        memset(credential, 0, sizeof(credential));
        if (!ok) snprintf(reason, reason_size, "connect-failed");
        return ok;
    }
    if (!network || network->saved_id < 0) {
        snprintf(reason, reason_size, "unknown-network");
        return false;
    }
    char id[32];
    snprintf(id, sizeof(id), "%d", network->saved_id);
    if (!strcmp(kind, "forget"))
        return run_program(env_or("WIFI_FORGET", "/usr/libexec/brick-wifi-forget"),
                           "network", id) == 0;
    if (!strcmp(kind, "set_auto_connect"))
        return wpa_ok("SET_NETWORK %d disabled %d", network->saved_id,
                      !strcmp(value, "1") ? 0 : 1) && wpa_ok("SAVE_CONFIG");
    if (!strcmp(kind, "set_private_address")) {
        bool current = network->current;
        if (current && (!wpa_ok("DISCONNECT") || !wait_disconnected())) return false;
        /* set-mode takes the requested mode as its third argument. */
        const char *mode = !strcmp(value, "1") ? "private" : "device";
        pid_t child = fork();
        if (child == 0) {
            execl(env_or("WIFI_PRIVATE_MAC", "/usr/libexec/brick-wifi-private-mac"),
                  "brick-wifi-private-mac", "set-mode", id, mode, (char *)NULL);
            _exit(127);
        }
        int status;
        if (child < 0 || waitpid(child, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status))
            return false;
        if (run_program(env_or("WIFI_PRIVATE_MAC", "/usr/libexec/brick-wifi-private-mac"),
                        "prepare", id) != 0) return false;
        return !current || wpa_ok("RECONNECT");
    }
    if (!strcmp(kind, "open_captive"))
        return run_program(env_or("WIFI_CAPTIVE_CONTROL", "/usr/bin/brick-captive-control"),
                           "open", id) == 0;
    if (!strcmp(kind, "recheck")) {
        return run_detached(env_or("WIFI_CONNECTIVITY", "/usr/libexec/brick-wifi-connectivity"),
                            "check");
    }
    snprintf(reason, reason_size, "unsupported-command");
    return false;
}

static void reason_hex(const char *reason, char *output, size_t size)
{
    size_t used = 0;
    append_hex(output, size, &used, (const unsigned char *)reason, strlen(reason));
}

static void process_packet(struct client *client, const char *packet)
{
    char version_text[16], generation_text[16], request_text[16];
    if (strncmp(packet, "command ", 8) ||
        !field_value(packet, "version", version_text, sizeof(version_text)) ||
        !field_value(packet, "generation", generation_text, sizeof(generation_text)) ||
        !field_value(packet, "request", request_text, sizeof(request_text)))
        return;
    char *end;
    unsigned long version = strtoul(version_text, &end, 10);
    if (*end || version != 1) return;
    unsigned long request = strtoul(request_text, &end, 10);
    if (*end || request == 0 || request > UINT32_MAX) return;
    unsigned long requested_generation = strtoul(generation_text, &end, 10);
    if (*end || requested_generation > UINT32_MAX) return;
    if ((uint32_t)request == client->last_request && client->last_result[0]) {
        send(client->fd, client->last_result, strlen(client->last_result), MSG_NOSIGNAL);
        return;
    }
    char status[16], reason[128] = "refresh";
    if ((uint32_t)requested_generation != generation)
        strcpy(status, "stale");
    else if (execute_command(packet, reason, sizeof(reason))) {
        strcpy(status, "accepted");
        reason[0] = '\0';
    } else {
        strcpy(status, "rejected");
        if (!reason[0]) strcpy(reason, "operation-failed");
    }
    char encoded[256] = "";
    reason_hex(reason, encoded, sizeof(encoded));
    snprintf(client->last_result, sizeof(client->last_result),
             "result generation=%u request=%lu status=%s reason_hex=%s\n",
             generation, request, status, encoded);
    client->last_request = (uint32_t)request;
    send(client->fd, client->last_result, strlen(client->last_result), MSG_NOSIGNAL);
}

static uint32_t new_generation(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint64_t value = (uint64_t)now.tv_nsec ^ ((uint64_t)now.tv_sec << 21) ^
                     ((uint64_t)getpid() << 7);
    value ^= value >> 33; value *= UINT64_C(0xff51afd7ed558ccd);
    value ^= value >> 33;
    uint32_t result = (uint32_t)value;
    return result ? result : 1;
}

int main(void)
{
    signal(SIGTERM, stop_handler);
    signal(SIGINT, stop_handler);
    signal(SIGPIPE, SIG_IGN);
    umask(077);
    generation = new_generation();
    revision = 1;
    const char *socket_path = env_or("WIFI_AUTHORITY_SOCKET", "/run/brick-wifi/control.sock");
    if (strlen(socket_path) >= sizeof(((struct sockaddr_un *)0)->sun_path))
        return 2;
    char state_dir[512];
    snprintf(state_dir, sizeof(state_dir), "%s", env_or("WIFI_STATE_DIR", "/run/brick-wifi"));
    if (mkdir(state_dir, 0700) < 0 && errno != EEXIST)
        return 1;
    int server = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (server < 0)
        return 1;
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    memcpy(address.sun_path, socket_path, strlen(socket_path) + 1);
    unlink(socket_path);
    if (bind(server, (struct sockaddr *)&address,
             offsetof(struct sockaddr_un, sun_path) + strlen(socket_path) + 1) < 0 ||
        chmod(socket_path, 0600) < 0 || listen(server, MAX_CLIENTS) < 0)
        return 1;
    struct client clients[MAX_CLIENTS];
    for (size_t index = 0; index < MAX_CLIENTS; index++) clients[index].fd = -1;
    char snapshot[MAX_PACKET] = "";
    size_t snapshot_length = 0;
    bool was_path_ready = false;
    char ready_bssid[sizeof(current_bssid)] = "";
    struct timespec disconnected_since = {0};
    bool recovery_attempted = false;
    while (!stopping) {
        char next[MAX_PACKET];
        build_snapshot(next, sizeof(next), revision);
        bool path_ready = !strcmp(current_state, "COMPLETED") && current_address[0];
        if (path_ready && was_path_ready && current_bssid[0] && ready_bssid[0] &&
            strcmp(current_bssid, ready_bssid)) {
            /* XR829 can complete a direct BSSID roam without delivering the
             * disconnect action used by S45wifi.  The old address may belong
             * to a different L2 domain, so independently replace its lease. */
            run_detached(env_or("WIFI_DHCP_SERVICE", "/etc/init.d/S45wifi"),
                         "dhcp-renew");
        }
        if (path_ready && current_bssid[0])
            snprintf(ready_bssid, sizeof(ready_bssid), "%s", current_bssid);
        if (path_ready && !was_path_ready) {
            const char *checker = env_or("WIFI_CONNECTIVITY",
                                         "/usr/libexec/brick-wifi-connectivity");
            /* Never show a prior network's verdict while this path is being
             * classified.  The check is detached from snapshot delivery. */
            run_program(checker, "clear", NULL);
            run_detached(checker, "check");
            build_snapshot(next, sizeof(next), revision);
        }
        was_path_ready = path_ready;
        bool disconnected = wifi_enabled() && !strcmp(radio_state(), "ready") &&
                            !strcmp(current_state, "DISCONNECTED");
        if (path_ready) {
            disconnected_since = (struct timespec){0};
            recovery_attempted = false;
        } else if (!disconnected) {
            disconnected_since = (struct timespec){0};
        } else {
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            if (!disconnected_since.tv_sec)
                disconnected_since = now;
            else if (!recovery_attempted &&
                     now.tv_sec - disconnected_since.tv_sec >= 5) {
                recovery_attempted = run_detached(
                    env_or("WIFI_RECOVER", "/usr/libexec/brick-wifi-recover"), NULL);
            }
        }
        bool changed = strcmp(next, snapshot) != 0;
        if (changed) {
            if (++revision == 0) revision = 1;
            snapshot_length = build_snapshot(snapshot, sizeof(snapshot), revision);
        }
        struct pollfd pollfds[MAX_CLIENTS + 1];
        pollfds[0] = (struct pollfd){ .fd = server, .events = POLLIN };
        for (size_t index = 0; index < MAX_CLIENTS; index++)
            pollfds[index + 1] = (struct pollfd){ .fd = clients[index].fd, .events = POLLIN };
        int ready = poll(pollfds, MAX_CLIENTS + 1, 1000);
        if (ready < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (changed)
            for (size_t index = 0; index < MAX_CLIENTS; index++)
                if (clients[index].fd >= 0 &&
                    send(clients[index].fd, snapshot, snapshot_length, MSG_NOSIGNAL) < 0) {
                    close(clients[index].fd); clients[index].fd = -1;
                }
        if (pollfds[0].revents & POLLIN) {
            int fd = accept4(server, NULL, NULL, SOCK_CLOEXEC);
            if (fd >= 0) {
                size_t index;
                for (index = 0; index < MAX_CLIENTS && clients[index].fd >= 0; index++);
                if (index == MAX_CLIENTS) close(fd);
                else {
                    clients[index] = (struct client){ .fd = fd };
                    send(fd, snapshot, snapshot_length, MSG_NOSIGNAL);
                }
            }
        }
        for (size_t index = 0; index < MAX_CLIENTS; index++) {
            if (clients[index].fd < 0) continue;
            short events = pollfds[index + 1].revents;
            if (events & (POLLERR | POLLHUP | POLLNVAL)) {
                close(clients[index].fd); clients[index].fd = -1; continue;
            }
            if (events & POLLIN) {
                char packet[MAX_PACKET];
                ssize_t length = recv(clients[index].fd, packet, sizeof(packet) - 1, 0);
                if (length <= 0) {
                    close(clients[index].fd); clients[index].fd = -1; continue;
                }
                packet[length] = '\0';
                process_packet(&clients[index], packet);
                explicit_bzero(packet, sizeof(packet));
                revision++;
                snapshot_length = build_snapshot(snapshot, sizeof(snapshot), revision);
                for (size_t peer = 0; peer < MAX_CLIENTS; peer++)
                    if (clients[peer].fd >= 0)
                        send(clients[peer].fd, snapshot, snapshot_length, MSG_NOSIGNAL);
            }
        }
    }
    for (size_t index = 0; index < MAX_CLIENTS; index++) if (clients[index].fd >= 0) close(clients[index].fd);
    close(server);
    unlink(socket_path);
    return 0;
}
