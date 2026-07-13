use uuid::Uuid;

pub(crate) const CLIENT_ID_ENV: &str = "SCRIBER_OUTLOOK_CLIENT_ID";
const BUILT_IN_CLIENT_ID: Option<&str> = option_env!("SCRIBER_OUTLOOK_CLIENT_ID");
const CALLBACK_PATH: &str = "api/calendar/outlook/callback";

pub(crate) fn normalize_client_id(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.len() != 36 {
        return None;
    }
    let parsed = Uuid::parse_str(trimmed).ok()?;
    if parsed.is_nil()
        || !parsed
            .hyphenated()
            .to_string()
            .eq_ignore_ascii_case(trimmed)
    {
        return None;
    }
    Some(parsed.hyphenated().to_string())
}

pub(crate) fn resolve_client_id(built_in: Option<&str>, runtime: Option<&str>) -> Option<String> {
    built_in
        .and_then(normalize_client_id)
        .or_else(|| runtime.and_then(normalize_client_id))
}

pub(crate) fn configured_client_id() -> Option<String> {
    let runtime = std::env::var(CLIENT_ID_ENV).ok();
    resolve_client_id(BUILT_IN_CLIENT_ID, runtime.as_deref())
}

pub(crate) fn is_valid_redirect_uri(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("http://localhost:") else {
        return false;
    };
    let Some((port, path)) = rest.split_once('/') else {
        return false;
    };
    port.parse::<u16>().is_ok_and(|port| port > 0) && path == CALLBACK_PATH
}

#[cfg(test)]
mod tests {
    use super::{is_valid_redirect_uri, normalize_client_id, resolve_client_id};

    const BUILT_IN: &str = "11111111-1111-4111-8111-111111111111";
    const RUNTIME: &str = "22222222-2222-4222-8222-222222222222";

    #[test]
    fn outlook_client_id_requires_a_non_nil_canonical_guid() {
        assert_eq!(normalize_client_id(BUILT_IN).as_deref(), Some(BUILT_IN));
        assert_eq!(
            normalize_client_id("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA").as_deref(),
            Some("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        );
        for invalid in [
            "",
            "not-a-guid",
            "{11111111-1111-4111-8111-111111111111}",
            "11111111111141118111111111111111",
            "00000000-0000-0000-0000-000000000000",
        ] {
            assert_eq!(normalize_client_id(invalid), None, "accepted {invalid}");
        }
    }

    #[test]
    fn built_in_outlook_client_id_wins_and_runtime_is_a_dev_fallback() {
        assert_eq!(
            resolve_client_id(Some(BUILT_IN), Some(RUNTIME)).as_deref(),
            Some(BUILT_IN)
        );
        assert_eq!(
            resolve_client_id(None, Some(RUNTIME)).as_deref(),
            Some(RUNTIME)
        );
        assert_eq!(
            resolve_client_id(Some("invalid"), Some(RUNTIME)).as_deref(),
            Some(RUNTIME)
        );
        assert_eq!(resolve_client_id(None, Some("invalid")), None);
    }

    #[test]
    fn outlook_redirect_accepts_only_the_registered_localhost_callback_shape() {
        assert!(is_valid_redirect_uri(
            "http://localhost:8765/api/calendar/outlook/callback"
        ));
        assert!(is_valid_redirect_uri(
            "http://localhost:49152/api/calendar/outlook/callback"
        ));
        for invalid in [
            "http://127.0.0.1:8765/api/calendar/outlook/callback",
            "https://localhost:8765/api/calendar/outlook/callback",
            "http://localhost:0/api/calendar/outlook/callback",
            "http://localhost:8765/api/calendar/outlook/callback/extra",
            "http://localhost:8765/api/calendar/outlook/callback?code=leak",
            "http://localhost.evil.example:8765/api/calendar/outlook/callback",
            "http://user@localhost:8765/api/calendar/outlook/callback",
        ] {
            assert!(!is_valid_redirect_uri(invalid), "accepted {invalid}");
        }
    }
}
