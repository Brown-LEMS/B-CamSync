#pragma once

#ifdef __linux__
#include <arpa/inet.h>
#include <cstdint>
#include <ifaddrs.h>
#include <net/if.h>
#include <stdexcept>
#include <string>

namespace bcamsync::detail {
/// Select an active interface's unicast IPv4, preferring non-link-local addresses.
inline std::string select_link_address(const ifaddrs* addresses, const std::string& interface,
                                       bool link_local_only = false) {
    std::uint32_t selected = 0;
    for (auto entry = addresses; entry; entry = entry->ifa_next) {
        if (!entry->ifa_name || interface != entry->ifa_name || !entry->ifa_addr ||
            entry->ifa_addr->sa_family != AF_INET ||
            (entry->ifa_flags & (IFF_UP | IFF_RUNNING)) != (IFF_UP | IFF_RUNNING) ||
            (entry->ifa_flags & IFF_LOOPBACK))
            continue;
        const auto address = ntohl(reinterpret_cast<const sockaddr_in*>(entry->ifa_addr)->sin_addr.s_addr);
        if ((address >> 24) == 0 || (address >> 24) == 127 || (address >> 28) >= 14)
            continue;
        if ((entry->ifa_flags & IFF_BROADCAST) && entry->ifa_broadaddr &&
            entry->ifa_broadaddr->sa_family == AF_INET &&
            address == ntohl(reinterpret_cast<const sockaddr_in*>(entry->ifa_broadaddr)->sin_addr.s_addr))
            continue;
        const bool link_local = (address & 0xffff0000U) == 0xa9fe0000U;
        if (link_local_only && !link_local) continue;
        const bool selected_link_local = (selected & 0xffff0000U) == 0xa9fe0000U;
        if (!selected || (selected_link_local && !link_local) ||
            (selected_link_local == link_local && address < selected))
            selected = address;
    }
    if (!selected) return {};
    in_addr address{};
    address.s_addr = htonl(selected);
    char text[INET_ADDRSTRLEN];
    if (!inet_ntop(AF_INET, &address, text, sizeof(text)))
        throw std::runtime_error("cannot format synchronization IPv4 address");
    return text;
}
} // namespace bcamsync::detail
#endif
