import java.util.*;
import java.nio.charset.StandardCharsets;
import java.io.*;
import java.security.*;
import java.security.cert.*;
import javax.net.ssl.*;
import javax.naming.*;
import javax.naming.directory.*;
import javax.naming.ldap.*;

class DirectoryReport {
    static String quote(String value) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            if (c == '"' || c == '\\') out.append('\\').append(c);
            else if (c < 32) out.append(String.format("\\u%04x", (int)c));
            else out.append(c);
        }
        return out.append('"').toString();
    }
    static String attribute(Attributes attrs, String name) throws NamingException {
        Attribute value = attrs.get(name);
        return value == null || value.size() == 0 ? "" : value.get().toString();
    }
    static void trust(String pem) throws Exception {
        if (pem.isEmpty()) return;
        KeyStore store = KeyStore.getInstance(KeyStore.getDefaultType());
        store.load(null, null);
        int index = 0;
        for (var cert : CertificateFactory.getInstance("X.509").generateCertificates(
                new ByteArrayInputStream(pem.getBytes(StandardCharsets.UTF_8)))) {
            store.setCertificateEntry("ca-" + index++, cert);
        }
        if (index == 0) throw new CertificateException();
        var manager = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        manager.init(store);
        var tls = SSLContext.getInstance("TLS");
        tls.init(null, manager.getTrustManagers(), null);
        SSLContext.setDefault(tls);
    }
    static void execute(String url, String bind, String password, String ca, String base,
            String groupBase, String username, String filter, String[] names,
            String[] targets, int limit) {
        List<String> warnings = new ArrayList<>();
        List<String> users = new ArrayList<>();
        LdapContext ctx = null;
        String error = "";
        try {
            trust(ca);
            Hashtable<String,String> env = new Hashtable<>();
            env.put(Context.INITIAL_CONTEXT_FACTORY, "com.sun.jndi.ldap.LdapCtxFactory");
            env.put(Context.PROVIDER_URL, url);
            env.put(Context.SECURITY_AUTHENTICATION, "simple");
            env.put(Context.SECURITY_PRINCIPAL, bind);
            env.put(Context.SECURITY_CREDENTIALS, password);
            env.put(Context.REFERRAL, "ignore");
            env.put("com.sun.jndi.ldap.connect.timeout", "5000");
            env.put("com.sun.jndi.ldap.read.timeout", "10000");
            ctx = new InitialLdapContext(env, null);
            List<LdapName> dns = new ArrayList<>();
            for (String name : names) {
                String dn = new Rdn("CN", name) + "," + groupBase;
                dns.add(new LdapName(dn));
                try { ctx.getAttributes(dn, new String[]{"cn"}); }
                catch (NameNotFoundException ex) {
                    warnings.add(quote("Configured group not found: " + name));
                }
            }
            SearchControls sc = new SearchControls();
            sc.setSearchScope(SearchControls.SUBTREE_SCOPE);
            sc.setTimeLimit(10000);
            sc.setReturningAttributes(new String[]{username, "displayName", "cn", "mail", "memberOf"});
            byte[] cookie = null;
            boolean done = false;
            do {
                ctx.setRequestControls(new Control[]{new PagedResultsControl(500, cookie, true)});
                var rows = ctx.search(base, filter, sc);
                try {
                    while (rows.hasMore()) {
                        if (users.size() >= limit) {
                            warnings.add(quote("Account limit reached; narrow the scope or raise --limit."));
                            done = true;
                            break;
                        }
                        var attrs = rows.next().getAttributes();
                        List<String> memberships = new ArrayList<>();
                        List<String> otherGroups = new ArrayList<>();
                        Attribute memberOf = attrs.get("memberOf");
                        if (memberOf != null) {
                            for (int n = 0; n < memberOf.size(); n++) {
                                LdapName groupDn = new LdapName(memberOf.get(n).toString());
                                int match = dns.indexOf(groupDn);
                                if (match >= 0) memberships.add(quote(targets[match]));
                                else otherGroups.add("{\"name\":"
                                    + quote(groupDn.getRdn(groupDn.size() - 1).getValue().toString())
                                    + ",\"dn\":" + quote(groupDn.toString()) + "}");
                            }
                        }
                        String name = attribute(attrs, "displayName");
                        if (name.isEmpty()) name = attribute(attrs, "cn");
                        users.add("{\"username\":" + quote(attribute(attrs, username))
                            + ",\"name\":" + quote(name) + ",\"email\":" + quote(attribute(attrs, "mail"))
                            + ",\"groups\":[" + String.join(",", memberships)
                            + "],\"otherGroups\":[" + String.join(",", otherGroups) + "]}");
                    }
                } catch (PartialResultException ex) {
                    warnings.add(quote("Directory referrals were not followed; results may be incomplete."));
                    done = true;
                } finally { rows.close(); }
                cookie = null;
                boolean paged = false;
                Control[] controls = ctx.getResponseControls();
                if (controls != null) for (Control control : controls) {
                    if (control instanceof PagedResultsResponseControl response) {
                        cookie = response.getCookie();
                        paged = true;
                    }
                }
                if (!paged && !done) {
                    warnings.add(quote("LDAP did not confirm paged results; completeness is unknown."));
                }
            } while (!done && cookie != null && cookie.length > 0);
        } catch (Exception ex) {
            // Never print exception messages, stack traces or the bind environment.
            error = ex.getClass().getSimpleName();
        } finally {
            if (ctx != null) try { ctx.close(); } catch (NamingException ignored) {}
        }
        System.out.println("NEURWERK_AD_REPORT:{\"users\":[" + String.join(",", users)
            + "],\"warnings\":[" + String.join(",", warnings) + "],\"error\":" + quote(error) + "}");
    }
}
