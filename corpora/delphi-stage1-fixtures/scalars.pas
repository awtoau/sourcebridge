unit Scalars;

interface

function AddSigned(Left: Integer; Right: Integer): Integer;
function AddUnsigned(Left: Cardinal; Right: Cardinal): Cardinal;

implementation

function AddSigned(Left: Integer; Right: Integer): Integer;
begin
  Result := Left + Right;
end;

function AddUnsigned(Left: Cardinal; Right: Cardinal): Cardinal;
begin
  Result := Left + Right;
end;

end.
